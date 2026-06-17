const state = {
  sourceEntities: [],
  currentEntities: [],
  deviceBackups: [],
  corruptDatabases: [],
  corruptDatabaseTotal: 0,
  currentDbBackups: [],
  reports: [],
  sourcePage: {
    offset: 0,
    limit: 100,
    total: 0,
    filter: "",
  },
  backupPage: {
    offset: 0,
    limit: 100,
    total: 0,
    filter: "",
  },
  activeTab: "analysis",
  status: null,
  resumedJobId: null,
  currentJobId: null,
};

const ACTIVE_JOB_STATUSES = ["queued", "running", "cancelling"];
const CANCELLABLE_JOB_KINDS = ["upload", "load_backup", "load_corrupt_database", "refresh_cache", "import"];

const $ = (id) => document.getElementById(id);

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function appendLog(message) {
  const log = $("operationLog");
  const time = new Date().toLocaleTimeString();
  log.textContent = `${log.textContent}${log.textContent ? "\n" : ""}[${time}] ${message}`;
  log.scrollTop = log.scrollHeight;
}

function renderJobLog(job) {
  const log = $("operationLog");
  log.textContent = (job.logs || []).join("\n");
  log.scrollTop = log.scrollHeight;
}

function clearOperationLog() {
  $("operationLog").textContent = "";
}

function setProgress(percent, status, indeterminate = false) {
  const track = $("progressTrack");
  const bar = $("progressBar");
  track.classList.toggle("indeterminate", indeterminate);
  if (!indeterminate) {
    bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }
  $("progressStatus").textContent = status;
}

function finishProgress(status) {
  setProgress(100, status);
  window.setTimeout(() => setProgress(0, "Bereit"), 1200);
}

function setActiveTab(tabName, updateHash = true) {
  const isImport = tabName === "import";
  state.activeTab = isImport ? "import" : "analysis";

  $("analysisPage").hidden = isImport;
  $("importPage").hidden = !isImport;
  $("analysisPage").classList.toggle("active", !isImport);
  $("importPage").classList.toggle("active", isImport);

  $("analysisTabButton").classList.toggle("active", !isImport);
  $("importTabButton").classList.toggle("active", isImport);
  $("analysisTabButton").setAttribute("aria-selected", String(!isImport));
  $("importTabButton").setAttribute("aria-selected", String(isImport));

  if (updateHash) {
    window.history.replaceState(null, "", isImport ? "#import" : "#analyse");
  }
}

function setBusy(isBusy) {
  for (const id of [
    "uploadButton",
    "cacheButton",
    "clearCacheButton",
    "refreshBackupsButton",
    "loadBackupButton",
    "refreshCorruptDatabasesButton",
    "loadCorruptDatabaseButton",
    "previewButton",
    "importButton",
    "restoreDbButton",
    "refreshDbBackupsButton",
    "refreshReportsButton",
    "openReportButton",
    "reanalyzeCurrentDbButton",
    "snapshotCurrentDbButton",
    "checkpointCurrentDbButton",
  ]) {
    $(id).disabled = isBusy;
  }
}

function toast(message, type = "info") {
  const element = $("toast");
  element.textContent = message;
  element.className = type;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => {
    element.textContent = "";
    element.className = "";
  }, 4500);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || response.statusText);
    error.payload = payload;
    throw error;
  }
  return payload;
}

function uploadFileWithProgress(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", "api/upload?async=1");
    request.responseType = "json";
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.setRequestHeader("X-Filename", encodeURIComponent(file.name));

    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) {
        setProgress(25, "Upload laeuft", true);
        return;
      }
      const percent = Math.round((event.loaded / event.total) * 100);
      setProgress(percent, `Upload ${percent}%`);
    });

    request.addEventListener("load", () => {
      const payload = request.response || {};
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
      } else {
        reject(new Error(payload.error || request.statusText || "Upload fehlgeschlagen"));
      }
    });

    request.addEventListener("error", () => reject(new Error("Upload fehlgeschlagen")));
    request.addEventListener("abort", () => reject(new Error("Upload abgebrochen")));
    request.send(file);
  });
}

async function startJob(payload) {
  return api("api/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function jobStatusLabel(job) {
  if (!job) return "Laeuft";
  if (job.status === "cancelling") return "Abbruch laeuft";
  return ACTIVE_JOB_STATUSES.includes(job.status) ? job.title : job.status;
}

function updateCancelButton(job) {
  const button = $("cancelJobButton");
  const canShow = job && CANCELLABLE_JOB_KINDS.includes(job.kind) && ACTIVE_JOB_STATUSES.includes(job.status);
  button.hidden = !canShow;
  if (!canShow) {
    button.disabled = true;
    button.textContent = "Abbrechen";
    return;
  }
  button.disabled = job.status === "cancelling" || Boolean(job.cancel_requested);
  button.textContent = button.disabled ? "Wird abgebrochen" : "Abbrechen";
}

async function cancelActiveJob() {
  const jobId = state.currentJobId;
  if (!jobId) return;
  updateCancelButton({ id: jobId, kind: "import", status: "cancelling", cancel_requested: true });
  try {
    const job = await api(`api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    renderJobLog(job);
    setProgress(job.progress || 0, jobStatusLabel(job));
    updateCancelButton(job);
  } catch (error) {
    toast(error.message, "error");
  }
}

function handleOperationError(error, resultElement = null) {
  if (error?.cancelled) {
    setProgress(0, "Abgebrochen");
    appendLog("Job abgebrochen.");
    if (resultElement) {
      resultElement.textContent = "Job abgebrochen.";
    }
    toast("Job abgebrochen", "warn");
    return;
  }
  setProgress(0, "Fehler");
  appendLog(`Fehler: ${error.message}`);
  if (error.payload?.active_job) {
    appendLog(`Aktiver Job: ${error.payload.active_job.title} (${error.payload.active_job.status})`);
  }
  if (resultElement) {
    resultElement.textContent = error.message;
  }
  toast(error.message, "error");
}

async function waitForJob(job) {
  let current = job;
  state.currentJobId = current.id;
  try {
    while (true) {
      setProgress(current.progress || 0, jobStatusLabel(current));
      renderJobLog(current);
      updateCancelButton(current);
      if (current.status === "succeeded") {
        finishProgress("Fertig");
        return current.result;
      }
      if (current.status === "cancelled") {
        const error = new Error("Job abgebrochen");
        error.cancelled = true;
        throw error;
      }
      if (current.status === "failed") {
        setProgress(0, "Fehler");
        throw new Error(current.error || "Job fehlgeschlagen");
      }
      await sleep(700);
      current = await api(`api/jobs/${encodeURIComponent(current.id)}`);
    }
  } finally {
    if (state.currentJobId === job.id) {
      state.currentJobId = null;
    }
    updateCancelButton(null);
  }
}

function completionMessage(job) {
  if (!job) return "Job abgeschlossen";
  const messages = {
    upload: "Upload analysiert und zwischengespeichert",
    load_backup: "Backup analysiert und zwischengespeichert",
    load_corrupt_database: "Defekte DB als Quelle geladen. Entitaeten koennen jetzt importiert werden.",
    refresh_cache: "Zwischenspeicher aktualisiert",
    import: "Import abgeschlossen",
    restore_current_db: "Aktuelle DB wiederhergestellt",
    snapshot_current_db: "Snapshot der aktuellen DB erstellt",
    checkpoint_current_db: "Passiver WAL-Checkpoint abgeschlossen",
  };
  return messages[job.kind] || `${job.title || "Job"} abgeschlossen`;
}

async function resumeActiveJob(job) {
  if (!job || !ACTIVE_JOB_STATUSES.includes(job.status) || state.resumedJobId === job.id) {
    return;
  }

  state.resumedJobId = job.id;
  setBusy(true);
  clearOperationLog();
  setProgress(job.progress || 0, job.title || "Job wird fortgesetzt");
  renderJobLog(job);
  toast(`${job.title || "Job"} wird fortgesetzt`, "info");

  try {
    await waitForJob(job);
    state.sourcePage.offset = 0;
    await refreshStatus();
    if (job.kind === "load_corrupt_database") {
      setActiveTab("import");
    }
    toast(completionMessage(job), "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    state.resumedJobId = null;
    setBusy(false);
  }
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatBytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function setHealth(element, ok, emptyText) {
  element.textContent = emptyText || (ok ? "In Ordnung" : "Nicht in Ordnung");
  element.className = ok ? "good" : "bad";
}

function renderDiagnosticsList(element, items, emptyText) {
  if (!items || !items.length) {
    element.innerHTML = `<li class="severity-info"><strong>${escapeHtml(emptyText)}</strong></li>`;
    return;
  }
  element.innerHTML = items
    .map((item) => `
      <li class="severity-${escapeHtml(item.severity || "info")}">
        <strong>${escapeHtml(item.title || "Hinweis")}</strong>
        <span>${escapeHtml(item.detail || "")}</span>
      </li>
    `)
    .join("");
}

function renderCurrentDbDiagnostics(target) {
  const diagnostics = target?.diagnostics || {};
  renderDiagnosticsList($("currentDbProblems"), diagnostics.problems || [], "Keine Probleme erkannt");
  renderDiagnosticsList($("currentDbRecommendations"), diagnostics.recommendations || [], "Keine Aktion noetig");

  const sidecars = target?.sidecars || {};
  const detail = {
    path: target?.path,
    ok: target?.ok,
    readable: target?.readable,
    partial: target?.partial,
    error: target?.error,
    integrity: target?.integrity,
    quick_check: target?.quick_check,
    read_errors: target?.read_errors,
    foreign_key_errors: target?.foreign_key_errors,
    journal_mode: target?.journal_mode,
    page_count: target?.page_count,
    freelist_count: target?.freelist_count,
    schema_version: target?.schema_version,
    user_version: target?.user_version,
    sidecars,
    tables: target?.tables,
  };
  $("currentDbDiagnosticsDetails").textContent = JSON.stringify(detail, null, 2);

  const actions = diagnostics.actions || [];
  $("checkpointCurrentDbButton").disabled = !actions.includes("checkpoint");
}

function toApiDateTime(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

function renderStatus() {
  const status = state.status;
  if (!status) return;

  $("currentDbPath").textContent = status.options.database_path;

  const source = status.cache.analysis;
  const sourceKindLabels = {
    upload: "Upload",
    device_backup: "Backup",
    corrupt_database: "Defekte DB",
  };
  const sourceKind = sourceKindLabels[status.cache.meta?.source_kind] || "Quelle";
  if (source && source.exists) {
    if (source.partial || (!source.ok && source.readable)) {
      $("sourceHealth").textContent = "Teilweise lesbar";
      $("sourceHealth").className = "warn";
    } else {
      setHealth($("sourceHealth"), Boolean(source.ok));
    }
    const warnings = source.read_errors?.length ? ` / ${source.read_errors.length} Warnung(en)` : "";
    $("sourceDetails").textContent = `${sourceKind} / ${formatBytes(source.size_bytes)} / ${source.states_count || 0} States / ${source.statistics_count || 0} LTS${warnings}`;
  } else {
    $("sourceHealth").textContent = "Keine Datei";
    $("sourceHealth").className = "muted";
    $("sourceDetails").textContent = "";
  }

  const target = status.current_database;
  if (target && target.exists) {
    if (target.partial || (!target.ok && target.readable)) {
      $("targetHealth").textContent = "Teilweise lesbar";
      $("targetHealth").className = "warn";
    } else {
      setHealth($("targetHealth"), Boolean(target.ok));
    }
    const warnings = target.read_errors?.length ? ` / ${target.read_errors.length} Warnung(en)` : "";
    $("targetDetails").textContent = `${formatBytes(target.size_bytes)} / ${target.states_count || 0} States / ${target.statistics_count || 0} LTS${warnings}`;
  } else {
    $("targetHealth").textContent = "Nicht gefunden";
    $("targetHealth").className = "bad";
    $("targetDetails").textContent = "";
  }
  renderCurrentDbDiagnostics(target);

  const targetEntityCount = Number(target?.entities_count || state.currentEntities.length || 0);
  const sourceEntityCount = Number(source?.entities_count || 0);
  $("currentEntityCount").textContent = String(targetEntityCount);
  $("currentEntityDetails").textContent = target?.first_state
    ? `Aktuelle Instanz / ${formatDate(target.first_state)} - ${formatDate(target.last_state)}`
    : "Aktuelle Instanz / kein Recorder-Zeitraum";

  if (source && source.exists) {
    $("backupEntityCount").textContent = String(sourceEntityCount);
    const range = source.first_state ? `${formatDate(source.first_state)} - ${formatDate(source.last_state)}` : "Kein Zeitraum";
    $("backupEntityDetails").textContent = `Geladene Backup-DB / ${range}`;
  } else {
    $("backupEntityCount").textContent = "0";
    $("backupEntityDetails").textContent = "Keine Backup-DB geladen";
  }

  const badge = $("healthBadge");
  const targetOk = Boolean(target?.ok);
  const sourceLoaded = Boolean(source?.exists);
  const sourceOk = !sourceLoaded || Boolean(source?.ok);
  const allOk = targetOk && sourceOk;
  badge.textContent = allOk ? (sourceLoaded ? "Bereit" : "Aktuelle DB ok") : "Pruefen";
  badge.className = allOk ? "badge badge-good" : "badge badge-muted";
}

function renderSourceEntities() {
  const rows = state.sourceEntities
    .map((entity) => `
      <tr>
        <td><code>${escapeHtml(entity.entity_id)}</code></td>
        <td>${entity.states_count}</td>
        <td>${entity.statistics_count || 0}</td>
        <td>${escapeHtml(formatDate(entity.first_seen || entity.first_statistic))}</td>
        <td>${escapeHtml(formatDate(entity.last_seen || entity.last_statistic))}</td>
        <td><button class="small" data-source="${escapeHtml(entity.entity_id)}">Importieren</button></td>
      </tr>
    `)
    .join("");

  $("sourceEntities").innerHTML = rows || '<tr><td colspan="6" class="empty">Keine Entitaeten</td></tr>';

  for (const button of $("sourceEntities").querySelectorAll("button[data-source]")) {
    button.addEventListener("click", () => {
      $("sourceEntity").value = button.dataset.source;
      if (state.currentEntities.some((entity) => entity.entity_id === button.dataset.source)) {
        $("targetEntity").value = button.dataset.source;
      }
      renderMappingSuggestions().catch((error) => appendLog(`Mapping-Vorschlaege: ${error.message}`));
      $("targetEntity").focus();
    });
  }

  renderPager();
}

function renderPager() {
  const page = state.sourcePage;
  const from = page.total ? page.offset + 1 : 0;
  const to = Math.min(page.offset + page.limit, page.total);
  $("pageInfo").textContent = `${from}-${to} von ${page.total}`;
  $("prevPageButton").disabled = page.offset <= 0;
  $("nextPageButton").disabled = page.offset + page.limit >= page.total;
}

function renderTargetEntities() {
  $("targetEntityList").innerHTML = state.currentEntities
    .map((entity) => `<option value="${escapeHtml(entity.entity_id)}">${escapeHtml(entity.name || entity.entity_id)}</option>`)
    .join("");
}

function renderDeviceBackups() {
  const select = $("deviceBackupSelect");
  select.innerHTML = state.deviceBackups
    .map((file) => {
      const label = `${file.relative_path} (${formatBytes(file.size_bytes)}, ${formatDate(file.modified)})`;
      return `<option value="${escapeHtml(file.id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  const page = state.backupPage;
  const from = page.total ? page.offset + 1 : 0;
  const to = Math.min(page.offset + page.limit, page.total);
  $("backupListInfo").textContent = page.total
    ? `${from}-${to} von ${page.total} Datei(en) in /backup`
    : "Keine Backup-Dateien in /backup gefunden";
  $("prevBackupPageButton").disabled = page.offset <= 0;
  $("nextBackupPageButton").disabled = page.offset + page.limit >= page.total;
}

function renderCorruptDatabases() {
  const select = $("corruptDatabaseSelect");
  select.innerHTML = state.corruptDatabases
    .map((file) => {
      const sidecars = file.sidecar_count ? `, ${file.sidecar_count} Sidecar` : ", keine Sidecars";
      const readable = file.sqlite_header ? "SQLite" : "kein SQLite-Header";
      const label = `${file.relative_path} (${formatBytes(file.size_bytes)}${sidecars}, ${readable}, ${formatDate(file.modified)})`;
      return `<option value="${escapeHtml(file.id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  const total = Number(state.corruptDatabaseTotal || 0);
  $("corruptDatabaseInfo").textContent = total
    ? `${total} defekte Recorder-DB(s) gefunden`
    : "Keine *.corrupt Recorder-DB im aktuellen DB-Verzeichnis gefunden";
  $("loadCorruptDatabaseButton").disabled = !state.corruptDatabases.length;
}

function renderCurrentDbBackups() {
  $("currentDbBackupSelect").innerHTML = state.currentDbBackups
    .map((file) => {
      const label = `${file.name} (${formatBytes(file.size_bytes)}, ${formatDate(file.modified)})`;
      return `<option value="${escapeHtml(file.id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  $("currentDbBackupInfo").textContent = state.currentDbBackups.length
    ? `${state.currentDbBackups.length} Sicherung(en) geladen`
    : "Noch keine aktuelle-DB-Sicherung vorhanden";
}

function renderReports() {
  $("reportSelect").innerHTML = state.reports
    .map((report) => {
      const label = `${report.created_at || report.id}: ${report.source_entity_id || "-"} -> ${report.target_entity_id || "-"} (${report.states_inserted || 0} States)`;
      return `<option value="${escapeHtml(report.id)}">${escapeHtml(label)}</option>`;
    })
    .join("");
  if (!state.reports.length) {
    $("reportView").textContent = "Keine Reports vorhanden.";
  }
}

async function renderMappingSuggestions() {
  const source = $("sourceEntity").value.trim();
  const container = $("mappingSuggestions");
  container.innerHTML = "";
  if (!source) return;
  const payload = await api(`api/mapping/suggestions?source_entity_id=${encodeURIComponent(source)}&limit=6`);
  const suggestions = payload.suggestions || [];
  container.innerHTML = suggestions
    .map((entity) => `<button class="secondary small" type="button" data-target="${escapeHtml(entity.entity_id)}">${escapeHtml(entity.entity_id)}</button>`)
    .join("");
  for (const button of container.querySelectorAll("button[data-target]")) {
    button.addEventListener("click", () => {
      $("targetEntity").value = button.dataset.target;
    });
  }
}

async function fetchSourceEntities() {
  const params = new URLSearchParams({
    offset: String(state.sourcePage.offset),
    limit: String(state.sourcePage.limit),
    filter: state.sourcePage.filter,
  });
  const source = await api(`api/source/entities?${params.toString()}`);
  state.sourceEntities = source.entities || [];
  state.sourcePage = {
    offset: source.offset || 0,
    limit: source.limit || state.sourcePage.limit,
    total: source.total || 0,
    filter: source.filter || "",
  };
  renderSourceEntities();
}

async function refreshBackups() {
  try {
    const params = new URLSearchParams({
      offset: String(state.backupPage.offset),
      limit: String(state.backupPage.limit),
      filter: state.backupPage.filter,
    });
    appendLog("Backup-Liste wird aktualisiert.");
    const payload = await api(`api/backups?${params.toString()}`);
    state.deviceBackups = payload.files || [];
    state.backupPage = {
      offset: payload.offset || 0,
      limit: payload.limit || state.backupPage.limit,
      total: payload.total || 0,
      filter: payload.filter || "",
    };
    renderDeviceBackups();
    appendLog(`${state.backupPage.total} Backup-Datei(en) gefunden.`);
  } catch (error) {
    appendLog(`Fehler beim Laden der Backup-Liste: ${error.message}`);
    toast(error.message, "error");
  }
}

async function refreshCorruptDatabases() {
  try {
    appendLog("Defekte Recorder-DBs werden gesucht.");
    const payload = await api("api/corrupt-databases?limit=100");
    state.corruptDatabases = payload.files || [];
    state.corruptDatabaseTotal = payload.total || 0;
    renderCorruptDatabases();
    appendLog(`${state.corruptDatabaseTotal} defekte Recorder-DB(s) gefunden.`);
  } catch (error) {
    appendLog(`Fehler beim Suchen defekter DBs: ${error.message}`);
    toast(error.message, "error");
  }
}

async function refreshCurrentDbBackups() {
  const payload = await api("api/current-db-backups?limit=100");
  state.currentDbBackups = payload.files || [];
  renderCurrentDbBackups();
}

async function refreshReports() {
  const payload = await api("api/reports?limit=50");
  state.reports = payload.reports || [];
  renderReports();
}

async function refreshStatus() {
  state.status = await api("api/status");

  const current = await api("api/current/entities");
  state.currentEntities = current.entities || [];
  renderTargetEntities();
  renderStatus();

  await fetchSourceEntities();
  await refreshCurrentDbBackups();
  await refreshReports();
}

async function uploadSelectedFile() {
  const file = $("fileInput").files[0];
  if (!file) {
    toast("Keine Datei ausgewaehlt", "warn");
    return;
  }
  setBusy(true);
  clearOperationLog();
  setProgress(0, "Upload startet");
  appendLog(`Upload gestartet: ${file.name} (${formatBytes(file.size)})`);
  try {
    const job = await uploadFileWithProgress(file);
    appendLog("Upload abgeschlossen. Server-Analyse startet.");
    await waitForJob(job);
    state.sourcePage.offset = 0;
    await refreshStatus();
    toast("Upload analysiert und zwischengespeichert", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
  }
}

async function loadDeviceBackup() {
  const fileId = $("deviceBackupSelect").value;
  if (!fileId) {
    toast("Keine Backup-Datei ausgewaehlt", "warn");
    return;
  }

  setBusy(true);
  clearOperationLog();
  setProgress(5, "Backup-Job startet");
  try {
    const job = await startJob({ action: "load_backup", file_id: fileId });
    await waitForJob(job);
    state.sourcePage.offset = 0;
    await refreshStatus();
    toast("Backup analysiert und zwischengespeichert", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
  }
}

async function loadCorruptDatabase() {
  const fileId = $("corruptDatabaseSelect").value;
  if (!fileId) {
    toast("Keine defekte Recorder-DB ausgewaehlt", "warn");
    return;
  }

  setBusy(true);
  clearOperationLog();
  setProgress(5, "Rettungs-Job startet");
  try {
    const job = await startJob({ action: "load_corrupt_database", file_id: fileId });
    await waitForJob(job);
    state.sourcePage.offset = 0;
    await refreshStatus();
    setActiveTab("import");
    toast("Defekte DB als Quelle geladen. Entitaeten koennen jetzt importiert werden.", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
  }
}

async function refreshCache() {
  setBusy(true);
  clearOperationLog();
  setProgress(5, "Cache-Job startet");
  try {
    const job = await startJob({ action: "refresh_cache" });
    await waitForJob(job);
    state.sourcePage.offset = 0;
    await refreshStatus();
    toast("Zwischenspeicher aktualisiert", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
  }
}

async function clearCache() {
  setBusy(true);
  clearOperationLog();
  setProgress(30, "Cache wird geleert", true);
  appendLog("Zwischenspeicher wird geleert.");
  try {
    await api("api/cache/clear", { method: "POST" });
    state.sourcePage.offset = 0;
    state.sourceEntities = [];
    await refreshStatus();
    appendLog("Zwischenspeicher geleert.");
    finishProgress("Fertig");
    toast("Cache geleert", "success");
  } catch (error) {
    setProgress(0, "Fehler");
    appendLog(`Fehler: ${error.message}`);
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function buildImportPayload(dryRun) {
  return {
    source_entity_id: $("sourceEntity").value.trim(),
    target_entity_id: $("targetEntity").value.trim(),
    dry_run: dryRun,
    confirm: $("confirmImport").checked,
    include_statistics: $("includeStatistics").checked,
    duplicate_strategy: $("duplicateStrategy").value,
    start: toApiDateTime($("startTime").value),
    end: toApiDateTime($("endTime").value),
  };
}

function validateImportPayload(payload) {
  if (!payload.source_entity_id || !payload.target_entity_id) {
    toast("Quelle und Ziel fehlen", "warn");
    return false;
  }
  if (!payload.dry_run && !payload.confirm) {
    toast("Schreibimport bestaetigen", "warn");
    return false;
  }
  return true;
}

async function previewImport() {
  const payload = buildImportPayload(true);
  if (!validateImportPayload(payload)) return;

  setBusy(true);
  clearOperationLog();
  setProgress(30, "Vorabpruefung", true);
  appendLog(`Vorabpruefung gestartet: ${payload.source_entity_id} -> ${payload.target_entity_id}`);
  $("importResult").textContent = "Laeuft...";
  try {
    const result = await api("api/import/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("importResult").textContent = JSON.stringify(result, null, 2);
    appendLog(`Pruefung fertig: ${result.preview.inserted} moegliche States, ${result.preview.skipped} uebersprungen.`);
    if (result.warnings?.length) {
      appendLog(`Warnungen: ${result.warnings.join(" | ")}`);
    }
    finishProgress("Pruefung fertig");
    toast("Vorabpruefung abgeschlossen", "success");
  } catch (error) {
    handleOperationError(error, $("importResult"));
  } finally {
    setBusy(false);
  }
}

async function runImport() {
  const payload = buildImportPayload(false);
  if (!validateImportPayload(payload)) return;

  setBusy(true);
  clearOperationLog();
  setProgress(5, "Import-Job startet");
  $("importResult").textContent = "Laeuft...";
  try {
    const job = await startJob({ action: "import", payload });
    const result = await waitForJob(job);
    $("importResult").textContent = JSON.stringify(result, null, 2);
    appendLog(`States: ${result.inserted} neu, ${result.skipped} uebersprungen, ${result.replaced || 0} ersetzt, ${result.scanned} geprueft.`);
    if (result.partial || result.read_errors?.length || result.source_warnings?.length) {
      appendLog("Teilimport: Die Quelle hatte Lesewarnungen. Details stehen im JSON-Ergebnis/Report.");
    }
    if (result.statistics) {
      appendLog(`Statistik: ${result.statistics.inserted} neu, ${result.statistics.skipped} uebersprungen, ${result.statistics.replaced || 0} ersetzt.`);
      if (result.statistics.partial || result.statistics.read_errors?.length) {
        appendLog("Teilimport Statistik: Einige Statistikbereiche waren nicht lesbar.");
      }
    }
    await refreshStatus();
    toast("Import abgeschlossen", "success");
  } catch (error) {
    handleOperationError(error, $("importResult"));
  } finally {
    setBusy(false);
  }
}

async function restoreCurrentDb() {
  const backupId = $("currentDbBackupSelect").value;
  if (!backupId) {
    toast("Keine DB-Sicherung ausgewaehlt", "warn");
    return;
  }
  if (!$("confirmRestore").checked) {
    toast("Restore bestaetigen", "warn");
    return;
  }

  setBusy(true);
  clearOperationLog();
  setProgress(5, "Restore-Job startet");
  try {
    const job = await startJob({ action: "restore_current_db", backup_id: backupId, confirm: true });
    const result = await waitForJob(job);
    $("reportView").textContent = JSON.stringify(result, null, 2);
    await refreshStatus();
    toast("Aktuelle DB wiederhergestellt. Home Assistant Neustart empfohlen.", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
  }
}

async function reanalyzeCurrentDb() {
  setBusy(true);
  clearOperationLog();
  setProgress(25, "Aktuelle DB wird geprueft", true);
  appendLog("Aktuelle Datenbankdiagnose wird neu geladen.");
  try {
    await refreshStatus();
    finishProgress("Pruefung fertig");
    toast("Aktuelle DB neu geprueft", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
  }
}

async function snapshotCurrentDb() {
  setBusy(true);
  clearOperationLog();
  setProgress(5, "Snapshot-Job startet");
  try {
    const job = await startJob({ action: "snapshot_current_db" });
    const result = await waitForJob(job);
    await refreshStatus();
    $("currentDbDiagnosticsDetails").textContent = JSON.stringify(result, null, 2);
    appendLog(`Snapshot erstellt: ${result.snapshot_path}`);
    toast("Aktuelle DB-Snapshot erstellt und analysiert", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
  }
}

async function checkpointCurrentDb() {
  if (!$("confirmDbMaintenance").checked) {
    toast("DB-Wartung bestaetigen", "warn");
    return;
  }

  setBusy(true);
  clearOperationLog();
  setProgress(5, "WAL-Checkpoint startet");
  try {
    const job = await startJob({ action: "checkpoint_current_db", mode: "PASSIVE", confirm: true });
    const result = await waitForJob(job);
    await refreshStatus();
    $("currentDbDiagnosticsDetails").textContent = JSON.stringify(result, null, 2);
    toast("Passiver WAL-Checkpoint abgeschlossen", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
    renderCurrentDbDiagnostics(state.status?.current_database);
  }
}

async function openSelectedReport() {
  const reportId = $("reportSelect").value;
  if (!reportId) {
    toast("Kein Report ausgewaehlt", "warn");
    return;
  }
  try {
    const report = await api(`api/reports/${encodeURIComponent(reportId)}`);
    $("reportView").textContent = JSON.stringify(report, null, 2);
  } catch (error) {
    toast(error.message, "error");
  }
}

function bindEvents() {
  $("analysisTabButton").addEventListener("click", () => setActiveTab("analysis"));
  $("importTabButton").addEventListener("click", () => setActiveTab("import"));
  $("cancelJobButton").addEventListener("click", cancelActiveJob);
  $("uploadButton").addEventListener("click", uploadSelectedFile);
  $("cacheButton").addEventListener("click", refreshCache);
  $("clearCacheButton").addEventListener("click", clearCache);
  $("refreshBackupsButton").addEventListener("click", refreshBackups);
  $("loadBackupButton").addEventListener("click", loadDeviceBackup);
  $("refreshCorruptDatabasesButton").addEventListener("click", refreshCorruptDatabases);
  $("loadCorruptDatabaseButton").addEventListener("click", loadCorruptDatabase);
  $("refreshDbBackupsButton").addEventListener("click", () => refreshCurrentDbBackups().catch((error) => toast(error.message, "error")));
  $("restoreDbButton").addEventListener("click", restoreCurrentDb);
  $("refreshReportsButton").addEventListener("click", () => refreshReports().catch((error) => toast(error.message, "error")));
  $("openReportButton").addEventListener("click", openSelectedReport);
  $("reanalyzeCurrentDbButton").addEventListener("click", reanalyzeCurrentDb);
  $("snapshotCurrentDbButton").addEventListener("click", snapshotCurrentDb);
  $("checkpointCurrentDbButton").addEventListener("click", checkpointCurrentDb);
  $("sourceFilter").addEventListener("input", () => {
    window.clearTimeout(bindEvents.filterTimer);
    bindEvents.filterTimer = window.setTimeout(() => {
      state.sourcePage.filter = $("sourceFilter").value.trim();
      state.sourcePage.offset = 0;
      fetchSourceEntities().catch((error) => toast(error.message, "error"));
    }, 250);
  });
  $("backupFilter").addEventListener("input", () => {
    window.clearTimeout(bindEvents.backupFilterTimer);
    bindEvents.backupFilterTimer = window.setTimeout(() => {
      state.backupPage.filter = $("backupFilter").value.trim();
      state.backupPage.offset = 0;
      refreshBackups().catch((error) => toast(error.message, "error"));
    }, 250);
  });
  $("sourceEntity").addEventListener("input", () => {
    window.clearTimeout(bindEvents.suggestionTimer);
    bindEvents.suggestionTimer = window.setTimeout(() => {
      renderMappingSuggestions().catch(() => {
        $("mappingSuggestions").innerHTML = "";
      });
    }, 350);
  });
  $("prevPageButton").addEventListener("click", () => {
    state.sourcePage.offset = Math.max(0, state.sourcePage.offset - state.sourcePage.limit);
    fetchSourceEntities().catch((error) => toast(error.message, "error"));
  });
  $("nextPageButton").addEventListener("click", () => {
    state.sourcePage.offset += state.sourcePage.limit;
    fetchSourceEntities().catch((error) => toast(error.message, "error"));
  });
  $("prevBackupPageButton").addEventListener("click", () => {
    state.backupPage.offset = Math.max(0, state.backupPage.offset - state.backupPage.limit);
    refreshBackups().catch((error) => toast(error.message, "error"));
  });
  $("nextBackupPageButton").addEventListener("click", () => {
    state.backupPage.offset += state.backupPage.limit;
    refreshBackups().catch((error) => toast(error.message, "error"));
  });
  $("pageSizeSelect").addEventListener("change", () => {
    state.sourcePage.limit = Number($("pageSizeSelect").value);
    state.sourcePage.offset = 0;
    fetchSourceEntities().catch((error) => toast(error.message, "error"));
  });
  $("previewButton").addEventListener("click", previewImport);
  $("importButton").addEventListener("click", runImport);
}

async function initialize() {
  refreshBackups().catch((error) => toast(error.message, "error"));
  refreshCorruptDatabases().catch((error) => toast(error.message, "error"));
  try {
    await refreshStatus();
    await resumeActiveJob(state.status?.active_job);
  } catch (error) {
    toast(error.message, "error");
  }
}

setActiveTab(window.location.hash === "#import" ? "import" : "analysis", false);
bindEvents();
initialize();
