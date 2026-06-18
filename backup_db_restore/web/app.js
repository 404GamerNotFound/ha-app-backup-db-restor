const state = {
  sourceEntities: [],
  currentEntities: [],
  currentDbEntities: [],
  currentTopEntities: [],
  selectedCurrentEntityIds: new Set(),
  currentPurgePreview: null,
  currentPurgePreviewKey: "",
  deviceBackups: [],
  corruptDatabases: [],
  corruptDatabaseTotal: 0,
  currentDbBackups: [],
  configBackups: [],
  reports: [],
  sourcePage: {
    offset: 0,
    limit: 100,
    total: 0,
    filter: "",
  },
  currentDbPage: {
    offset: 0,
    limit: 100,
    total: 0,
    filter: "",
    sort: "entity_id",
    order: "asc",
  },
  backupPage: {
    offset: 0,
    limit: 100,
    total: 0,
    filter: "",
  },
  corruptPage: {
    offset: 0,
    limit: 100,
    total: 0,
    filter: "",
  },
  activeTab: "analysis",
  status: null,
  settings: null,
  resumedJobId: null,
  currentJobId: null,
  isBusy: false,
};

const ACTIVE_JOB_STATUSES = ["queued", "running", "cancelling"];
const CANCELLABLE_JOB_KINDS = ["upload", "load_backup", "load_corrupt_database", "refresh_cache", "import", "config_backup", "restore_config_backup"];

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

function sourceKindLabel(kind) {
  const labels = {
    upload: "Upload",
    device_backup: "Backup",
    corrupt_database: "Defekte DB",
  };
  return labels[kind] || "Quelle";
}

function setActiveTab(tabName, updateHash = true) {
  const activeTab = ["import", "config", "settings"].includes(tabName) ? tabName : "analysis";
  state.activeTab = activeTab;

  const tabs = {
    analysis: { page: "analysisPage", button: "analysisTabButton", hash: "#analyse" },
    import: { page: "importPage", button: "importTabButton", hash: "#import" },
    config: { page: "configBackupPage", button: "configBackupTabButton", hash: "#konfig-backup" },
    settings: { page: "settingsPage", button: "settingsTabButton", hash: "#einstellungen" },
  };
  for (const [name, item] of Object.entries(tabs)) {
    const isActive = name === activeTab;
    $(item.page).hidden = !isActive;
    $(item.page).classList.toggle("active", isActive);
    $(item.button).classList.toggle("active", isActive);
    $(item.button).setAttribute("aria-selected", String(isActive));
  }

  if (updateHash) {
    window.history.replaceState(null, "", tabs[activeTab].hash);
  }
}

function setBusy(isBusy) {
  state.isBusy = isBusy;
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
    "currentEntityFilter",
    "currentEntitySortSelect",
    "currentEntityOrderSelect",
    "currentEntitySelectAll",
    "clearCurrentSelectionButton",
    "currentPurgeRangeMode",
    "currentPurgeStart",
    "currentPurgeEnd",
    "currentPurgeOlderDays",
    "cleanupCurrentMetadata",
    "currentPurgeMaintenance",
    "previewCurrentPurgeButton",
    "executeCurrentPurgeButton",
    "prevCurrentEntityPageButton",
    "nextCurrentEntityPageButton",
    "currentEntityPageSizeSelect",
    "createConfigBackupButton",
    "refreshConfigBackupsButton",
    "inspectConfigBackupButton",
    "downloadConfigBackupButton",
    "uploadConfigBackupButton",
    "previewConfigRestoreButton",
    "restoreConfigBackupButton",
    "reloadSettingsButton",
    "saveSettingsButton",
  ]) {
    $(id).disabled = isBusy;
  }
  setCurrentPurgeButtonsBusy(isBusy);
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

function uploadConfigBackupWithProgress(file) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", "api/config-backups/upload");
    request.responseType = "json";
    request.setRequestHeader("Content-Type", "application/gzip");
    request.setRequestHeader("X-Filename", encodeURIComponent(file.name));

    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) {
        setProgress(25, "Archiv-Upload laeuft", true);
        return;
      }
      const percent = Math.round((event.loaded / event.total) * 100);
      setProgress(percent, `Archiv-Upload ${percent}%`);
    });

    request.addEventListener("load", () => {
      const payload = request.response || {};
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
      } else {
        reject(new Error(payload.error || request.statusText || "Archiv-Upload fehlgeschlagen"));
      }
    });

    request.addEventListener("error", () => reject(new Error("Archiv-Upload fehlgeschlagen")));
    request.addEventListener("abort", () => reject(new Error("Archiv-Upload abgebrochen")));
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
    purge_entity_history: "Entity-History geloescht",
    config_backup: "Konfig-Backup erstellt",
    restore_config_backup: "Konfig-Backup wiederhergestellt",
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
    } else if (job.kind === "config_backup" || job.kind === "restore_config_backup") {
      setActiveTab("config");
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

function formatNumber(value) {
  return Number(value || 0).toLocaleString();
}

function entityDatapoints(entity) {
  if (entity?.datapoints_count !== undefined && entity?.datapoints_count !== null) {
    return Number(entity.datapoints_count || 0);
  }
  return Number(entity?.states_count || 0)
    + Number(entity?.statistics_count || 0)
    + Number(entity?.statistics_short_term_count || 0);
}

function currentSelectionIds() {
  return [...state.selectedCurrentEntityIds].sort();
}

function currentPurgeRangePayload() {
  const mode = $("currentPurgeRangeMode").value;
  if (mode === "before") {
    return { start: null, end: toApiDateTime($("currentPurgeEnd").value) };
  }
  if (mode === "after") {
    return { start: toApiDateTime($("currentPurgeStart").value), end: null };
  }
  if (mode === "between") {
    return {
      start: toApiDateTime($("currentPurgeStart").value),
      end: toApiDateTime($("currentPurgeEnd").value),
    };
  }
  if (mode === "older_than") {
    const days = Math.max(1, Number($("currentPurgeOlderDays").value || 90));
    return { start: null, end: new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString() };
  }
  return { start: null, end: null };
}

function buildCurrentPurgePayload() {
  const range = currentPurgeRangePayload();
  return {
    entity_ids: currentSelectionIds(),
    start: range.start,
    end: range.end,
    cleanup_metadata: $("cleanupCurrentMetadata").checked,
    maintenance: $("currentPurgeMaintenance").value,
  };
}

function currentPurgePayloadKey() {
  return JSON.stringify({
    entity_ids: currentSelectionIds(),
    mode: $("currentPurgeRangeMode").value,
    start: $("currentPurgeStart").value || null,
    end: $("currentPurgeEnd").value || null,
    older_days: $("currentPurgeOlderDays").value || null,
    cleanup_metadata: $("cleanupCurrentMetadata").checked,
    maintenance: $("currentPurgeMaintenance").value,
  });
}

function purgeRangeLabel(range) {
  if (!range?.start && !range?.end) return "Gesamte History";
  if (range.start && range.end) return `${formatDate(range.start)} - ${formatDate(range.end)}`;
  if (range.start) return `ab ${formatDate(range.start)}`;
  return `bis ${formatDate(range.end)}`;
}

function currentPurgeRangeValid() {
  const mode = $("currentPurgeRangeMode").value;
  if (mode === "before") return Boolean($("currentPurgeEnd").value);
  if (mode === "after") return Boolean($("currentPurgeStart").value);
  if (mode === "between") return Boolean($("currentPurgeStart").value && $("currentPurgeEnd").value);
  if (mode === "older_than") return Number($("currentPurgeOlderDays").value || 0) > 0;
  return true;
}

function resetCurrentPurgePreview() {
  state.currentPurgePreview = null;
  state.currentPurgePreviewKey = "";
  renderCurrentPurgePreview();
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

function renderUploadFileInfo() {
  const file = $("fileInput").files[0];
  $("uploadFileInfo").textContent = file
    ? `${file.name} (${formatBytes(file.size)})`
    : "Keine Datei ausgewaehlt";
}

function renderConfigBackupUploadInfo() {
  const file = $("configBackupUploadInput").files[0];
  $("configBackupUploadInfo").textContent = file
    ? `${file.name} (${formatBytes(file.size)})`
    : "Kein Archiv ausgewaehlt";
}

function detailItem(label, value) {
  if (value === null || value === undefined || value === "") return "";
  return `
    <div>
      <strong>${escapeHtml(label)}</strong>
      <span>${escapeHtml(value)}</span>
    </div>
  `;
}

function renderSourceMetaDetails(status) {
  const container = $("sourceMetaDetails");
  const meta = status?.cache?.meta;
  const analysis = status?.cache?.analysis;
  const storage = status?.cache?.storage || {};
  const cacheDetails = [
    detailItem("Cache-Pfad", status?.cache?.cache_dir || status?.options?.cache_path),
    detailItem("Freier Cache-Speicher", storage.free_bytes === null || storage.free_bytes === undefined ? "" : formatBytes(storage.free_bytes)),
  ];
  if (!meta || !analysis?.exists) {
    container.innerHTML = [
      '<div><strong>Keine Quelle geladen</strong><span>Upload, Backup oder defekte DB als Quelle laden.</span></div>',
      ...cacheDetails,
    ].join("");
    return;
  }

  const sidecars = meta.recovery_sidecars || {};
  const warnings = meta.recovery_warnings || [];
  const sidecarSummary = Object.keys(sidecars).length
    ? Object.entries(sidecars).map(([kind, info]) => `${kind}: ${formatBytes(info.size_bytes || 0)}`).join(", ")
    : "";
  const extract = meta.extract || {};
  container.innerHTML = [
    detailItem("Typ", sourceKindLabel(meta.source_kind)),
    detailItem("Name", meta.original_name),
    detailItem("Pfad", meta.original_path),
    detailItem("Archiv-Mitglied", extract.selected_member),
    detailItem("Sidecars", sidecarSummary),
    detailItem("Warnungen", warnings.join(" | ")),
    detailItem("Gecacht", formatDate(meta.cached_at)),
    ...cacheDetails,
  ].join("") || '<div><strong>Quelle geladen</strong><span>Keine weiteren Details vorhanden.</span></div>';
}

function renderImportReadiness() {
  const source = $("sourceEntity").value.trim();
  const target = $("targetEntity").value.trim();
  const hasSourceDb = Boolean(state.status?.cache?.analysis?.exists);
  const hasSourceEntity = source && state.sourceEntities.some((entity) => entity.entity_id === source);
  const targetKnown = target && state.currentEntities.some((entity) => entity.entity_id === target);
  const problems = [];
  if (!hasSourceDb) problems.push("keine Quelle geladen");
  if (!source) problems.push("Quelle fehlt");
  if (source && !hasSourceEntity) problems.push("Quelle nicht in der geladenen DB-Liste");
  if (!target) problems.push("Ziel fehlt");
  if (target && !targetKnown) problems.push("Ziel ist aktuell nicht bekannt");
  if (!$("dryRun").checked && !$("confirmImport").checked) problems.push("Schreibimport nicht bestaetigt");

  const element = $("importReadiness");
  if (!problems.length) {
    element.textContent = "Bereit fuer Vorabpruefung oder Import.";
    element.className = "readiness readiness-good";
    return;
  }
  element.textContent = `Noch offen: ${problems.join(", ")}`;
  element.className = target && !targetKnown ? "readiness readiness-warn" : "readiness";
}

function renderStatus() {
  const status = state.status;
  if (!status) return;

  $("currentDbPath").textContent = `DB: ${status.options.database_path} / Cache: ${status.options.cache_path}`;

  const source = status.cache.analysis;
  const sourceKind = sourceKindLabel(status.cache.meta?.source_kind);
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
  renderSourceMetaDetails(status);
  renderConfigBackupStatus(status);
  renderSettingsStatus(status.settings);
  renderImportReadiness();
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
      renderImportReadiness();
      renderMappingSuggestions().catch((error) => appendLog(`Mapping-Vorschlaege: ${error.message}`));
      $("targetEntity").focus();
    });
  }

  renderPager();
}

function startImportFromCurrentEntity(entityId) {
  $("targetEntity").value = entityId;
  if (state.sourceEntities.some((entity) => entity.entity_id === entityId)) {
    $("sourceEntity").value = entityId;
  }
  setActiveTab("import");
  renderImportReadiness();
  renderMappingSuggestions().catch((error) => appendLog(`Mapping-Vorschlaege: ${error.message}`));
  $("sourceEntity").focus();
}

async function prepareCurrentEntityPurge(entityId) {
  state.selectedCurrentEntityIds.clear();
  state.selectedCurrentEntityIds.add(entityId);
  resetCurrentPurgePreview();
  renderCurrentDbEntities();
  document.querySelector(".purge-controls")?.scrollIntoView({ behavior: "smooth", block: "center" });
  await previewCurrentEntityHistoryPurge();
}

function renderCurrentTopEntities() {
  const container = $("currentTopEntities");
  if (!state.currentTopEntities.length) {
    container.innerHTML = '<div class="empty">Keine Speicherfresser gefunden</div>';
    return;
  }

  container.innerHTML = state.currentTopEntities
    .map((entity, index) => {
      const datapoints = entityDatapoints(entity);
      const disabled = state.isBusy ? " disabled" : "";
      return `
        <div class="top-entity-row">
          <strong class="top-entity-rank">${index + 1}</strong>
          <div class="top-entity-main">
            <code class="top-entity-id" title="${escapeHtml(entity.entity_id)}">${escapeHtml(entity.entity_id)}</code>
            <span class="top-entity-count">${formatNumber(datapoints)} Datenpunkte</span>
          </div>
          <div class="top-entity-actions">
            <button class="secondary small purge-shortcut" type="button" data-current-purge="${escapeHtml(entity.entity_id)}"${disabled}>Purge</button>
            <button class="secondary small" type="button" data-current-import="${escapeHtml(entity.entity_id)}"${disabled}>Import</button>
          </div>
        </div>
      `;
    })
    .join("");

  for (const button of container.querySelectorAll("button[data-current-purge]")) {
    button.addEventListener("click", () => {
      prepareCurrentEntityPurge(button.dataset.currentPurge).catch((error) => toast(error.message, "error"));
    });
  }

  for (const button of container.querySelectorAll("button[data-current-import]")) {
    button.addEventListener("click", () => startImportFromCurrentEntity(button.dataset.currentImport));
  }
}

function renderCurrentDbEntities() {
  const rows = state.currentDbEntities
    .map((entity) => {
      const datapoints = entityDatapoints(entity);
      const checked = state.selectedCurrentEntityIds.has(entity.entity_id) ? " checked" : "";
      const disabled = state.isBusy ? " disabled" : "";
      return `
        <tr>
          <td><input class="current-entity-select" type="checkbox" value="${escapeHtml(entity.entity_id)}"${checked}${disabled} aria-label="${escapeHtml(entity.entity_id)} auswaehlen"></td>
          <td><code>${escapeHtml(entity.entity_id)}</code></td>
          <td>${formatNumber(datapoints)}</td>
          <td>${formatNumber(entity.states_count)}</td>
          <td>${formatNumber(entity.statistics_count || 0)} / ${formatNumber(entity.statistics_short_term_count || 0)}</td>
          <td>${escapeHtml(formatDate(entity.first_seen || entity.first_statistic))}</td>
          <td>${escapeHtml(formatDate(entity.last_seen || entity.last_statistic))}</td>
          <td><button class="secondary small" type="button" data-current-import="${escapeHtml(entity.entity_id)}"${disabled}>Import</button></td>
        </tr>
      `;
    })
    .join("");

  $("currentDbEntities").innerHTML = rows || '<tr><td colspan="8" class="empty">Keine Entitaeten</td></tr>';

  for (const checkbox of $("currentDbEntities").querySelectorAll(".current-entity-select")) {
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        state.selectedCurrentEntityIds.add(checkbox.value);
      } else {
        state.selectedCurrentEntityIds.delete(checkbox.value);
      }
      resetCurrentPurgePreview();
      renderCurrentPurgeControls();
    });
  }

  for (const button of $("currentDbEntities").querySelectorAll("button[data-current-import]")) {
    button.addEventListener("click", () => startImportFromCurrentEntity(button.dataset.currentImport));
  }

  setCurrentPurgeButtonsBusy(state.isBusy);
  renderCurrentDbPager();
  renderCurrentPurgeControls();
}

function setCurrentPurgeButtonsBusy(isBusy) {
  for (const checkbox of document.querySelectorAll(".current-entity-select")) {
    checkbox.disabled = isBusy;
  }
  for (const button of document.querySelectorAll("button[data-current-import]")) {
    button.disabled = isBusy;
  }
  for (const button of document.querySelectorAll("button[data-current-purge]")) {
    button.disabled = isBusy;
  }
  renderCurrentPurgeControls();
}

function renderPager() {
  const page = state.sourcePage;
  const from = page.total ? page.offset + 1 : 0;
  const to = Math.min(page.offset + page.limit, page.total);
  $("pageInfo").textContent = `${from}-${to} von ${page.total}`;
  $("prevPageButton").disabled = page.offset <= 0;
  $("nextPageButton").disabled = page.offset + page.limit >= page.total;
}

function renderCurrentDbPager() {
  const page = state.currentDbPage;
  const from = page.total ? page.offset + 1 : 0;
  const to = Math.min(page.offset + page.limit, page.total);
  $("currentEntityPageInfo").textContent = `${from}-${to} von ${page.total}`;
  $("prevCurrentEntityPageButton").disabled = page.offset <= 0;
  $("nextCurrentEntityPageButton").disabled = page.offset + page.limit >= page.total;
}

function renderCurrentPurgeControls() {
  const selectedCount = state.selectedCurrentEntityIds.size;
  const mode = $("currentPurgeRangeMode").value;
  $("currentEntitySelectionInfo").textContent = `${formatNumber(selectedCount)} ausgewaehlt`;

  $("currentPurgeStart").disabled = state.isBusy || !["after", "between"].includes(mode);
  $("currentPurgeEnd").disabled = state.isBusy || !["before", "between"].includes(mode);
  $("currentPurgeOlderDays").disabled = state.isBusy || mode !== "older_than";

  const visibleIds = state.currentDbEntities.map((entity) => entity.entity_id);
  const visibleSelected = visibleIds.filter((entityId) => state.selectedCurrentEntityIds.has(entityId));
  const selectAll = $("currentEntitySelectAll");
  selectAll.disabled = state.isBusy || visibleIds.length === 0;
  selectAll.checked = visibleIds.length > 0 && visibleSelected.length === visibleIds.length;
  selectAll.indeterminate = visibleSelected.length > 0 && visibleSelected.length < visibleIds.length;

  const validRange = currentPurgeRangeValid();
  const previewMatches = state.currentPurgePreview && state.currentPurgePreviewKey === currentPurgePayloadKey();
  const previewDeleted = state.currentPurgePreview?.deleted || {};
  const previewTotal = Number(previewDeleted.total_datapoints || 0)
    + Number(previewDeleted.states_meta || 0)
    + Number(previewDeleted.statistics_meta || 0);
  $("previewCurrentPurgeButton").disabled = state.isBusy || selectedCount === 0 || !validRange;
  $("executeCurrentPurgeButton").disabled = state.isBusy || !previewMatches || previewTotal <= 0;
  $("clearCurrentSelectionButton").disabled = state.isBusy || selectedCount === 0;
}

function renderCurrentPurgePreview() {
  const container = $("currentPurgePreview");
  const preview = state.currentPurgePreview;
  if (!preview) {
    container.innerHTML = "";
    renderCurrentPurgeControls();
    return;
  }

  const deleted = preview.deleted || {};
  const entityLines = (preview.entities || [])
    .slice(0, 8)
    .map((entity) => `${entity.entity_id}: ${formatNumber(entity.deleted?.total_datapoints || 0)}`)
    .join(", ");
  const more = (preview.entities || []).length > 8 ? `, +${(preview.entities || []).length - 8}` : "";
  const warnings = preview.warnings?.length ? `<span>${escapeHtml(preview.warnings.join(" | "))}</span>` : "";
  container.innerHTML = `
    <div>
      <strong>${formatNumber(deleted.total_datapoints || 0)} Datenpunkt(e)</strong>
      <span>${formatNumber(deleted.states || 0)} States / ${formatNumber(deleted.statistics || 0)} LTS / ${formatNumber(deleted.statistics_short_term || 0)} Kurzzeit</span>
    </div>
    <div>
      <strong>${formatNumber(preview.entity_count || 0)} Entitaet(en)</strong>
      <span>${escapeHtml(entityLines + more)}</span>
    </div>
    <div>
      <strong>Zeitraum</strong>
      <span>${escapeHtml(purgeRangeLabel(preview.time_range))}</span>
    </div>
    <div>
      <strong>Groesse</strong>
      <span>${formatBytes(preview.estimated_selected_bytes || 0)} geschaetzt / DB ${formatBytes(preview.database_size_bytes || 0)}</span>
    </div>
    <div>
      <strong>Metadaten</strong>
      <span>${preview.cleanup_metadata ? `${formatNumber(deleted.states_meta || 0)} States-Meta / ${formatNumber(deleted.statistics_meta || 0)} Statistik-Meta` : "Nicht bereinigen"}</span>
    </div>
    <div>
      <strong>Wartung</strong>
      <span>${escapeHtml(preview.maintenance || "none")}</span>
    </div>
    ${warnings ? `<div><strong>Warnung</strong>${warnings}</div>` : ""}
  `;
  renderCurrentPurgeControls();
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
  const page = state.corruptPage;
  const from = page.total ? page.offset + 1 : 0;
  const to = Math.min(page.offset + page.limit, page.total);
  $("corruptDatabaseInfo").textContent = page.total
    ? `${from}-${to} von ${page.total} defekte DB(s)`
    : "Keine *.corrupt Recorder-DB im aktuellen DB-Verzeichnis gefunden";
  $("prevCorruptPageButton").disabled = page.offset <= 0;
  $("nextCorruptPageButton").disabled = page.offset + page.limit >= page.total;
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

function renderConfigBackupStatus(status) {
  const config = status?.config_backup || {};
  const storage = config.storage || {};
  $("configBackupStorage").textContent = storage.free_bytes === null || storage.free_bytes === undefined
    ? "Unbekannt"
    : formatBytes(storage.free_bytes);
  $("configBackupDetails").textContent = `${config.backup_dir || "-"} / Config: ${config.config_dir || "-"}`;
  $("configBackupCount").textContent = String(state.configBackups.length);
  $("configBackupCountDetails").textContent = state.configBackups.length
    ? `${state.configBackups.length} Archiv(e) im Zielverzeichnis`
    : "Noch keine Konfig-Backups geladen";
  $("configBackupLocationDetails").innerHTML = [
    detailItem("Backup-Ziel", config.backup_dir),
    detailItem("HA-Config", config.config_dir),
    detailItem("Freier Speicher", storage.free_bytes === null || storage.free_bytes === undefined ? "" : formatBytes(storage.free_bytes)),
  ].join("");
}

function restartLabel(key) {
  const labels = {
    cache_path: "Cache-Pfad",
    log_level: "Log-Level",
  };
  return labels[key] || key;
}

function renderSettingsStorage(statusId, detailsId, storage, path, restartPending = false) {
  const element = $(statusId);
  const details = $(detailsId);
  const hasError = Boolean(storage?.error);
  const hasFreeSpace = storage?.free_bytes !== null && storage?.free_bytes !== undefined;
  const canUse = !hasError && (storage?.exists ? (storage?.readable && storage?.writable) : hasFreeSpace);
  if (restartPending) {
    element.textContent = "Neustart offen";
    element.className = "warn";
  } else if (canUse) {
    element.textContent = hasFreeSpace ? formatBytes(storage.free_bytes) : "Bereit";
    element.className = "good";
  } else {
    element.textContent = "Pruefen";
    element.className = "bad";
  }
  const suffix = restartPending ? " / wird nach Neustart aktiv" : "";
  const error = storage?.error ? ` / ${storage.error}` : "";
  details.textContent = `${path || storage?.path || "-"}${suffix}${error}`;
}

function renderSettingsStatus(settings) {
  if (!settings) return;
  state.settings = settings;
  const options = settings.options || {};
  const restartRequired = settings.restart_required || [];
  const cacheRestart = restartRequired.includes("cache_path");
  const configStorage = settings.storage?.config_backup_path || {};
  const cacheStorage = settings.storage?.configured_cache_path || settings.storage?.cache_path || {};

  $("settingDatabasePath").value = options.database_path || "";
  $("settingCachePath").value = options.cache_path || "";
  $("settingConfigBackupPath").value = options.config_backup_path || "";
  $("settingMaxUploadMb").value = options.max_upload_mb || 131072;
  $("settingLogLevel").value = options.log_level || "info";
  $("settingCreateCurrentDbBackup").checked = Boolean(options.create_current_db_backup);

  renderSettingsStorage("settingsCacheStatus", "settingsCacheDetails", cacheStorage, options.cache_path, cacheRestart);
  renderSettingsStorage("settingsConfigBackupStatus", "settingsConfigBackupDetails", configStorage, options.config_backup_path, false);

  $("settingsDetails").innerHTML = [
    detailItem("Aktiver Cache", settings.effective?.cache_path),
    detailItem("Aktives Konfig-Backup-Ziel", settings.effective?.config_backup_path),
    detailItem("Upload-Limit", `${options.max_upload_mb || 131072} MB`),
    detailItem("Neustart erforderlich", restartRequired.length ? restartRequired.map(restartLabel).join(", ") : "Nein"),
  ].join("");
  $("settingsResult").textContent = JSON.stringify(settings, null, 2);
}

function configBackupLabel(file) {
  const labels = file.component_labels?.length ? file.component_labels.join(", ") : "Konfig";
  const secretMarker = file.include_secrets ? " / Secrets" : "";
  const count = file.file_count === null || file.file_count === undefined ? "?" : file.file_count;
  return `${file.created_at || file.modified || file.name}: ${labels}${secretMarker} (${count} Datei(en), ${formatBytes(file.size_bytes)})`;
}

function renderConfigBackups() {
  const select = $("configBackupSelect");
  select.innerHTML = state.configBackups
    .map((file) => `<option value="${escapeHtml(file.id)}">${escapeHtml(configBackupLabel(file))}</option>`)
    .join("");
  const hasBackups = state.configBackups.length > 0;
  $("configBackupListInfo").textContent = hasBackups
    ? `${state.configBackups.length} Konfig-Backup(s) geladen`
    : "Noch keine Konfig-Backups vorhanden";
  $("configBackupCount").textContent = String(state.configBackups.length);
  $("configBackupCountDetails").textContent = hasBackups
    ? `${state.configBackups.length} Archiv(e) im Zielverzeichnis`
    : "Noch keine Konfig-Backups geladen";
  for (const id of ["inspectConfigBackupButton", "downloadConfigBackupButton", "previewConfigRestoreButton", "restoreConfigBackupButton"]) {
    $(id).disabled = !hasBackups;
  }
  if (hasBackups && $("configBackupResult").textContent === "Keine Konfig-Backups vorhanden.") {
    $("configBackupResult").textContent = "";
  }
  if (!hasBackups && !$("configBackupResult").textContent) {
    $("configBackupResult").textContent = "Keine Konfig-Backups vorhanden.";
  }
}

function renderReports() {
  $("reportSelect").innerHTML = state.reports
    .map((report) => {
      const kind = report.kind === "purge" ? "Purge" : "Import";
      const count = report.kind === "purge"
        ? `${formatNumber(report.states_deleted || 0)} States geloescht`
        : `${formatNumber(report.states_inserted || 0)} States`;
      const label = `${report.created_at || report.id}: ${kind} ${report.source_entity_id || "-"} -> ${report.target_entity_id || "-"} (${count})`;
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
  if (!suggestions.length) {
    container.innerHTML = '<span class="muted-line">Keine Zielvorschlaege gefunden.</span>';
    return;
  }
  container.innerHTML = suggestions
    .map((entity) => {
      const reasons = entity.reasons?.length ? ` title="${escapeHtml(entity.reasons.join(", "))}"` : "";
      return `<button class="secondary small" type="button" data-target="${escapeHtml(entity.entity_id)}"${reasons}>${escapeHtml(entity.entity_id)}</button>`;
    })
    .join("");
  for (const button of container.querySelectorAll("button[data-target]")) {
    button.addEventListener("click", () => {
      $("targetEntity").value = button.dataset.target;
      renderImportReadiness();
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
  renderImportReadiness();
}

async function fetchCurrentDbEntities() {
  const params = new URLSearchParams({
    offset: String(state.currentDbPage.offset),
    limit: String(state.currentDbPage.limit),
    filter: state.currentDbPage.filter,
    sort: state.currentDbPage.sort,
    order: state.currentDbPage.order,
  });
  const payload = await api(`api/current/entities?${params.toString()}`);
  state.currentDbEntities = payload.entities || [];
  state.currentDbPage = {
    offset: payload.offset || 0,
    limit: payload.limit || state.currentDbPage.limit,
    total: payload.total || 0,
    filter: payload.filter || "",
    sort: payload.sort || state.currentDbPage.sort,
    order: payload.order || state.currentDbPage.order,
  };
  $("currentEntitySortSelect").value = state.currentDbPage.sort;
  $("currentEntityOrderSelect").value = state.currentDbPage.order;
  renderCurrentDbEntities();
}

async function fetchCurrentTopEntities() {
  const params = new URLSearchParams({
    offset: "0",
    limit: "10",
    filter: "",
    sort: "datapoints",
    order: "desc",
  });
  const payload = await api(`api/current/entities?${params.toString()}`);
  state.currentTopEntities = payload.entities || [];
  renderCurrentTopEntities();
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
    const params = new URLSearchParams({
      offset: String(state.corruptPage.offset),
      limit: String(state.corruptPage.limit),
      filter: state.corruptPage.filter,
    });
    appendLog("Defekte Recorder-DBs werden gesucht.");
    const payload = await api(`api/corrupt-databases?${params.toString()}`);
    state.corruptDatabases = payload.files || [];
    state.corruptDatabaseTotal = payload.total || 0;
    state.corruptPage = {
      offset: payload.offset || 0,
      limit: payload.limit || state.corruptPage.limit,
      total: payload.total || 0,
      filter: payload.filter || "",
    };
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

async function refreshConfigBackups() {
  const payload = await api("api/config-backups?limit=100");
  state.configBackups = payload.files || [];
  renderConfigBackups();
  if (state.status) {
    state.status.config_backup = {
      ...(state.status.config_backup || {}),
      backup_dir: payload.backup_dir,
      config_dir: payload.config_dir,
      storage: payload.storage,
    };
    renderConfigBackupStatus(state.status);
  }
}

async function refreshSettings() {
  $("reloadSettingsButton").disabled = true;
  try {
    const settings = await api("api/settings");
    renderSettingsStatus(settings);
    toast("Einstellungen geladen", "success");
  } catch (error) {
    $("settingsResult").textContent = error.message;
    toast(error.message, "error");
  } finally {
    $("reloadSettingsButton").disabled = false;
  }
}

function buildSettingsPayload() {
  return {
    database_path: $("settingDatabasePath").value.trim(),
    cache_path: $("settingCachePath").value.trim(),
    config_backup_path: $("settingConfigBackupPath").value.trim(),
    max_upload_mb: Number($("settingMaxUploadMb").value),
    log_level: $("settingLogLevel").value,
    create_current_db_backup: $("settingCreateCurrentDbBackup").checked,
  };
}

async function saveSettings() {
  setBusy(true);
  $("settingsResult").textContent = "Einstellungen werden gespeichert...";
  try {
    const settings = await api("api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildSettingsPayload()),
    });
    renderSettingsStatus(settings);
    await refreshStatus();
    const restartRequired = settings.restart_required || [];
    toast(restartRequired.length ? "Gespeichert. Neustart erforderlich." : "Einstellungen gespeichert", "success");
  } catch (error) {
    $("settingsResult").textContent = error.message;
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function refreshStatus() {
  state.status = await api("api/status");

  const current = await api("api/current/entities");
  state.currentEntities = current.entities || [];
  renderTargetEntities();
  renderStatus();

  await fetchCurrentDbEntities();
  await fetchCurrentTopEntities();
  await fetchSourceEntities();
  await refreshCurrentDbBackups();
  await refreshConfigBackups();
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

async function previewCurrentEntityHistoryPurge() {
  const payload = buildCurrentPurgePayload();
  if (!payload.entity_ids.length) {
    toast("Mindestens eine Entitaet auswaehlen", "warn");
    return;
  }
  if (!currentPurgeRangeValid()) {
    toast("Zeitraum vervollstaendigen", "warn");
    return;
  }

  $("previewCurrentPurgeButton").disabled = true;
  $("currentPurgePreview").innerHTML = '<div><strong>Vorschau laedt</strong><span>Bitte warten...</span></div>';
  try {
    const preview = await api("api/current/purge-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.currentPurgePreview = preview;
    state.currentPurgePreviewKey = currentPurgePayloadKey();
    renderCurrentPurgePreview();
    toast("Purge-Vorschau geladen", "success");
  } catch (error) {
    state.currentPurgePreview = null;
    state.currentPurgePreviewKey = "";
    $("currentPurgePreview").innerHTML = "";
    toast(error.message, "error");
  } finally {
    renderCurrentPurgeControls();
  }
}

async function executeCurrentEntityHistoryPurge() {
  const payload = buildCurrentPurgePayload();
  const previewMatches = state.currentPurgePreview && state.currentPurgePreviewKey === currentPurgePayloadKey();
  if (!previewMatches) {
    toast("Bitte zuerst eine aktuelle Vorschau laden", "warn");
    return;
  }

  const preview = state.currentPurgePreview;
  const deleted = preview.deleted || {};
  const confirmed = window.confirm(
    `History-Purge wirklich ausfuehren?\n\n${formatNumber(preview.entity_count || 0)} Entitaet(en), ${formatNumber(deleted.total_datapoints || 0)} Datenpunkt(e), ${formatNumber((deleted.states_meta || 0) + (deleted.statistics_meta || 0))} Metadatenzeile(n), Zeitraum: ${purgeRangeLabel(preview.time_range)}`
  );
  if (!confirmed) return;

  setBusy(true);
  clearOperationLog();
  setProgress(5, "Purge-Job startet");
  appendLog(`History-Purge gestartet: ${formatNumber(payload.entity_ids.length)} Entitaet(en), ${purgeRangeLabel(preview.time_range)}`);
  try {
    const job = await startJob({ action: "purge_entity_history", payload, confirm: true });
    const result = await waitForJob(job);
    $("currentDbDiagnosticsDetails").textContent = JSON.stringify(result, null, 2);
    state.selectedCurrentEntityIds.clear();
    resetCurrentPurgePreview();
    await refreshStatus();
    const deleted = result.deleted || {};
    appendLog(`Purge fertig: ${deleted.total_datapoints || 0} Datenpunkt(e), ${(deleted.states_meta || 0) + (deleted.statistics_meta || 0)} Metadatenzeile(n) geloescht.`);
    if (result.report?.id) appendLog(`Purge-Report gespeichert: ${result.report.id}`);
    toast("Entity-History geloescht", "success");
  } catch (error) {
    handleOperationError(error);
  } finally {
    setBusy(false);
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

function selectedConfigBackupPayload() {
  const components = [...document.querySelectorAll(".config-component:checked")].map((element) => element.value);
  return {
    components,
    include_secrets: $("includeConfigSecrets").checked,
  };
}

async function createConfigBackup() {
  const payload = selectedConfigBackupPayload();
  if (!payload.components.length) {
    toast("Mindestens einen Konfigbereich auswaehlen", "warn");
    return;
  }

  setBusy(true);
  clearOperationLog();
  setProgress(5, "Konfig-Backup startet");
  $("configBackupResult").textContent = "Laeuft...";
  try {
    const job = await startJob({ action: "config_backup", payload });
    const result = await waitForJob(job);
    $("configBackupResult").textContent = JSON.stringify(result, null, 2);
    await refreshConfigBackups();
    toast("Konfig-Backup erstellt", "success");
  } catch (error) {
    handleOperationError(error, $("configBackupResult"));
  } finally {
    setBusy(false);
  }
}

async function inspectConfigBackup() {
  const backupId = $("configBackupSelect").value;
  if (!backupId) {
    toast("Kein Konfig-Backup ausgewaehlt", "warn");
    return;
  }
  try {
    const backup = await api(`api/config-backups/${encodeURIComponent(backupId)}`);
    $("configBackupResult").textContent = JSON.stringify(backup, null, 2);
  } catch (error) {
    toast(error.message, "error");
  }
}

function downloadConfigBackup() {
  const backupId = $("configBackupSelect").value;
  if (!backupId) {
    toast("Kein Konfig-Backup ausgewaehlt", "warn");
    return;
  }
  appendLog(`Konfig-Backup Download gestartet: ${backupId}`);
  window.location.href = `api/config-backups/${encodeURIComponent(backupId)}/download`;
}

async function uploadConfigBackup() {
  const file = $("configBackupUploadInput").files[0];
  if (!file) {
    toast("Kein Archiv ausgewaehlt", "warn");
    return;
  }
  setBusy(true);
  clearOperationLog();
  setProgress(0, "Archiv-Upload startet");
  $("configBackupResult").textContent = "Laeuft...";
  appendLog(`Konfig-Backup Upload gestartet: ${file.name} (${formatBytes(file.size)})`);
  try {
    const result = await uploadConfigBackupWithProgress(file);
    $("configBackupResult").textContent = JSON.stringify(result, null, 2);
    await refreshConfigBackups();
    finishProgress("Upload fertig");
    toast("Konfig-Backup hochgeladen", "success");
  } catch (error) {
    handleOperationError(error, $("configBackupResult"));
  } finally {
    setBusy(false);
    renderConfigBackups();
  }
}

async function previewConfigRestore() {
  const backupId = $("configBackupSelect").value;
  if (!backupId) {
    toast("Kein Konfig-Backup ausgewaehlt", "warn");
    return;
  }
  setBusy(true);
  clearOperationLog();
  setProgress(35, "Konfig-Restore wird geprueft", true);
  try {
    const preview = await api(`api/config-backups/${encodeURIComponent(backupId)}/preview`);
    $("configBackupResult").textContent = JSON.stringify(preview, null, 2);
    const counts = preview.counts || {};
    appendLog(`Restore-Vorschau: ${counts.changed || 0} geaendert, ${counts.new || 0} neu, ${counts.same || 0} unveraendert.`);
    finishProgress("Pruefung fertig");
    toast("Konfig-Restore geprueft", "success");
  } catch (error) {
    handleOperationError(error, $("configBackupResult"));
  } finally {
    setBusy(false);
    renderConfigBackups();
  }
}

async function restoreConfigBackup() {
  const backupId = $("configBackupSelect").value;
  if (!backupId) {
    toast("Kein Konfig-Backup ausgewaehlt", "warn");
    return;
  }
  if (!$("confirmConfigRestore").checked) {
    toast("Konfig-Restore bestaetigen", "warn");
    return;
  }

  setBusy(true);
  clearOperationLog();
  setProgress(5, "Konfig-Restore startet");
  $("configBackupResult").textContent = "Laeuft...";
  try {
    const job = await startJob({ action: "restore_config_backup", backup_id: backupId, confirm: true });
    const result = await waitForJob(job);
    $("configBackupResult").textContent = JSON.stringify(result, null, 2);
    await refreshConfigBackups();
    toast("Konfig wiederhergestellt. Home Assistant Neustart empfohlen.", "success");
  } catch (error) {
    handleOperationError(error, $("configBackupResult"));
  } finally {
    setBusy(false);
    renderConfigBackups();
  }
}

function bindEvents() {
  $("analysisTabButton").addEventListener("click", () => setActiveTab("analysis"));
  $("importTabButton").addEventListener("click", () => setActiveTab("import"));
  $("configBackupTabButton").addEventListener("click", () => setActiveTab("config"));
  $("settingsTabButton").addEventListener("click", () => setActiveTab("settings"));
  $("cancelJobButton").addEventListener("click", cancelActiveJob);
  $("fileInput").addEventListener("change", renderUploadFileInfo);
  $("configBackupUploadInput").addEventListener("change", renderConfigBackupUploadInfo);
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
  $("currentEntitySelectAll").addEventListener("change", () => {
    for (const entity of state.currentDbEntities) {
      if ($("currentEntitySelectAll").checked) {
        state.selectedCurrentEntityIds.add(entity.entity_id);
      } else {
        state.selectedCurrentEntityIds.delete(entity.entity_id);
      }
    }
    resetCurrentPurgePreview();
    renderCurrentDbEntities();
  });
  $("clearCurrentSelectionButton").addEventListener("click", () => {
    state.selectedCurrentEntityIds.clear();
    resetCurrentPurgePreview();
    renderCurrentDbEntities();
  });
  for (const id of ["currentPurgeRangeMode", "currentPurgeStart", "currentPurgeEnd", "currentPurgeOlderDays", "cleanupCurrentMetadata", "currentPurgeMaintenance"]) {
    $(id).addEventListener("input", () => {
      resetCurrentPurgePreview();
      renderCurrentPurgeControls();
    });
    $(id).addEventListener("change", () => {
      resetCurrentPurgePreview();
      renderCurrentPurgeControls();
    });
  }
  for (const id of ["currentEntitySortSelect", "currentEntityOrderSelect"]) {
    $(id).addEventListener("change", () => {
      state.currentDbPage.sort = $("currentEntitySortSelect").value;
      state.currentDbPage.order = $("currentEntityOrderSelect").value;
      state.currentDbPage.offset = 0;
      fetchCurrentDbEntities().catch((error) => toast(error.message, "error"));
    });
  }
  $("previewCurrentPurgeButton").addEventListener("click", previewCurrentEntityHistoryPurge);
  $("executeCurrentPurgeButton").addEventListener("click", executeCurrentEntityHistoryPurge);
  $("createConfigBackupButton").addEventListener("click", createConfigBackup);
  $("refreshConfigBackupsButton").addEventListener("click", () => refreshConfigBackups().catch((error) => toast(error.message, "error")));
  $("inspectConfigBackupButton").addEventListener("click", inspectConfigBackup);
  $("downloadConfigBackupButton").addEventListener("click", downloadConfigBackup);
  $("uploadConfigBackupButton").addEventListener("click", uploadConfigBackup);
  $("previewConfigRestoreButton").addEventListener("click", previewConfigRestore);
  $("restoreConfigBackupButton").addEventListener("click", restoreConfigBackup);
  $("reloadSettingsButton").addEventListener("click", refreshSettings);
  $("saveSettingsButton").addEventListener("click", saveSettings);
  $("sourceFilter").addEventListener("input", () => {
    window.clearTimeout(bindEvents.filterTimer);
    bindEvents.filterTimer = window.setTimeout(() => {
      state.sourcePage.filter = $("sourceFilter").value.trim();
      state.sourcePage.offset = 0;
      fetchSourceEntities().catch((error) => toast(error.message, "error"));
    }, 250);
  });
  $("currentEntityFilter").addEventListener("input", () => {
    window.clearTimeout(bindEvents.currentEntityFilterTimer);
    bindEvents.currentEntityFilterTimer = window.setTimeout(() => {
      state.currentDbPage.filter = $("currentEntityFilter").value.trim();
      state.currentDbPage.offset = 0;
      fetchCurrentDbEntities().catch((error) => toast(error.message, "error"));
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
  $("corruptDatabaseFilter").addEventListener("input", () => {
    window.clearTimeout(bindEvents.corruptFilterTimer);
    bindEvents.corruptFilterTimer = window.setTimeout(() => {
      state.corruptPage.filter = $("corruptDatabaseFilter").value.trim();
      state.corruptPage.offset = 0;
      refreshCorruptDatabases().catch((error) => toast(error.message, "error"));
    }, 250);
  });
  $("sourceEntity").addEventListener("input", () => {
    renderImportReadiness();
    window.clearTimeout(bindEvents.suggestionTimer);
    bindEvents.suggestionTimer = window.setTimeout(() => {
      renderMappingSuggestions().catch(() => {
        $("mappingSuggestions").innerHTML = "";
      });
    }, 350);
  });
  for (const id of ["targetEntity", "dryRun", "confirmImport", "includeStatistics"]) {
    $(id).addEventListener("input", renderImportReadiness);
    $(id).addEventListener("change", renderImportReadiness);
  }
  $("prevPageButton").addEventListener("click", () => {
    state.sourcePage.offset = Math.max(0, state.sourcePage.offset - state.sourcePage.limit);
    fetchSourceEntities().catch((error) => toast(error.message, "error"));
  });
  $("nextPageButton").addEventListener("click", () => {
    state.sourcePage.offset += state.sourcePage.limit;
    fetchSourceEntities().catch((error) => toast(error.message, "error"));
  });
  $("prevCurrentEntityPageButton").addEventListener("click", () => {
    state.currentDbPage.offset = Math.max(0, state.currentDbPage.offset - state.currentDbPage.limit);
    fetchCurrentDbEntities().catch((error) => toast(error.message, "error"));
  });
  $("nextCurrentEntityPageButton").addEventListener("click", () => {
    state.currentDbPage.offset += state.currentDbPage.limit;
    fetchCurrentDbEntities().catch((error) => toast(error.message, "error"));
  });
  $("prevBackupPageButton").addEventListener("click", () => {
    state.backupPage.offset = Math.max(0, state.backupPage.offset - state.backupPage.limit);
    refreshBackups().catch((error) => toast(error.message, "error"));
  });
  $("nextBackupPageButton").addEventListener("click", () => {
    state.backupPage.offset += state.backupPage.limit;
    refreshBackups().catch((error) => toast(error.message, "error"));
  });
  $("prevCorruptPageButton").addEventListener("click", () => {
    state.corruptPage.offset = Math.max(0, state.corruptPage.offset - state.corruptPage.limit);
    refreshCorruptDatabases().catch((error) => toast(error.message, "error"));
  });
  $("nextCorruptPageButton").addEventListener("click", () => {
    state.corruptPage.offset += state.corruptPage.limit;
    refreshCorruptDatabases().catch((error) => toast(error.message, "error"));
  });
  $("pageSizeSelect").addEventListener("change", () => {
    state.sourcePage.limit = Number($("pageSizeSelect").value);
    state.sourcePage.offset = 0;
    fetchSourceEntities().catch((error) => toast(error.message, "error"));
  });
  $("currentEntityPageSizeSelect").addEventListener("change", () => {
    state.currentDbPage.limit = Number($("currentEntityPageSizeSelect").value);
    state.currentDbPage.offset = 0;
    fetchCurrentDbEntities().catch((error) => toast(error.message, "error"));
  });
  $("previewButton").addEventListener("click", previewImport);
  $("importButton").addEventListener("click", runImport);
}

async function initialize() {
  renderUploadFileInfo();
  renderConfigBackupUploadInfo();
  renderConfigBackups();
  renderImportReadiness();
  renderCurrentTopEntities();
  renderCurrentPurgeControls();
  refreshBackups().catch((error) => toast(error.message, "error"));
  refreshCorruptDatabases().catch((error) => toast(error.message, "error"));
  try {
    await refreshStatus();
    await resumeActiveJob(state.status?.active_job);
  } catch (error) {
    toast(error.message, "error");
  }
}

function tabFromHash(hash) {
  if (hash === "#import") return "import";
  if (hash === "#konfig-backup") return "config";
  if (hash === "#einstellungen" || hash === "#settings") return "settings";
  return "analysis";
}

setActiveTab(tabFromHash(window.location.hash), false);
bindEvents();
initialize();
