const state = {
  sourceEntities: [],
  currentEntities: [],
  deviceBackups: [],
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
  status: null,
};

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

function setBusy(isBusy) {
  for (const id of [
    "uploadButton",
    "cacheButton",
    "clearCacheButton",
    "refreshBackupsButton",
    "loadBackupButton",
    "previewButton",
    "importButton",
    "restoreDbButton",
    "refreshDbBackupsButton",
    "refreshReportsButton",
    "openReportButton",
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
    throw new Error(payload.error || response.statusText);
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

async function waitForJob(job) {
  let current = job;
  while (true) {
    const statusLabel = current.status === "running" ? current.title : current.status;
    setProgress(current.progress || 0, statusLabel || "Laeuft");
    renderJobLog(current);
    if (current.status === "succeeded") {
      finishProgress("Fertig");
      return current.result;
    }
    if (current.status === "failed") {
      setProgress(0, "Fehler");
      throw new Error(current.error || "Job fehlgeschlagen");
    }
    await sleep(700);
    current = await api(`api/jobs/${encodeURIComponent(current.id)}`);
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
  if (source && source.exists) {
    setHealth($("sourceHealth"), Boolean(source.ok));
    $("sourceDetails").textContent = `${formatBytes(source.size_bytes)} / ${source.states_count || 0} States / ${source.statistics_count || 0} LTS`;
  } else {
    $("sourceHealth").textContent = "Keine Datei";
    $("sourceHealth").className = "muted";
    $("sourceDetails").textContent = "";
  }

  const target = status.current_database;
  if (target && target.exists) {
    setHealth($("targetHealth"), Boolean(target.ok));
    $("targetDetails").textContent = `${formatBytes(target.size_bytes)} / ${target.states_count || 0} States / ${target.statistics_count || 0} LTS`;
  } else {
    $("targetHealth").textContent = "Nicht gefunden";
    $("targetHealth").className = "bad";
    $("targetDetails").textContent = "";
  }

  $("entityCount").textContent = String(source?.entities_count || 0);
  $("historyRange").textContent = source?.first_state ? `${formatDate(source.first_state)} - ${formatDate(source.last_state)}` : "";

  const badge = $("healthBadge");
  const allOk = Boolean(source?.ok) && Boolean(target?.ok);
  badge.textContent = allOk ? "Bereit" : "Pruefen";
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
  renderStatus();

  const current = await api("api/current/entities");
  state.currentEntities = current.entities || [];
  renderTargetEntities();

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
    setProgress(0, "Fehler");
    appendLog(`Fehler: ${error.message}`);
    toast(error.message, "error");
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
    setProgress(0, "Fehler");
    appendLog(`Fehler: ${error.message}`);
    toast(error.message, "error");
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
    setProgress(0, "Fehler");
    appendLog(`Fehler: ${error.message}`);
    toast(error.message, "error");
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
    setProgress(0, "Fehler");
    appendLog(`Fehler: ${error.message}`);
    $("importResult").textContent = error.message;
    toast(error.message, "error");
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
    if (result.statistics) {
      appendLog(`Statistik: ${result.statistics.inserted} neu, ${result.statistics.skipped} uebersprungen, ${result.statistics.replaced || 0} ersetzt.`);
    }
    await refreshStatus();
    toast("Import abgeschlossen", "success");
  } catch (error) {
    setProgress(0, "Fehler");
    appendLog(`Fehler: ${error.message}`);
    $("importResult").textContent = error.message;
    toast(error.message, "error");
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
    setProgress(0, "Fehler");
    appendLog(`Fehler: ${error.message}`);
    toast(error.message, "error");
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

function bindEvents() {
  $("uploadButton").addEventListener("click", uploadSelectedFile);
  $("cacheButton").addEventListener("click", refreshCache);
  $("clearCacheButton").addEventListener("click", clearCache);
  $("refreshBackupsButton").addEventListener("click", refreshBackups);
  $("loadBackupButton").addEventListener("click", loadDeviceBackup);
  $("refreshDbBackupsButton").addEventListener("click", () => refreshCurrentDbBackups().catch((error) => toast(error.message, "error")));
  $("restoreDbButton").addEventListener("click", restoreCurrentDb);
  $("refreshReportsButton").addEventListener("click", () => refreshReports().catch((error) => toast(error.message, "error")));
  $("openReportButton").addEventListener("click", openSelectedReport);
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

bindEvents();
refreshBackups();
refreshStatus().catch((error) => toast(error.message, "error"));
