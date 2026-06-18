#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import copy
import hashlib
import io
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_APP_DIR", "/app"))
WEB_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_WEB_DIR", str(APP_DIR / "web")))
DATA_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_DATA_DIR", "/data"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_BACKUP_DIR", "/backup"))
CONFIG_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_CONFIG_DIR", "/homeassistant_config"))
OPTIONS_PATH = DATA_DIR / "options.json"
APP_VERSION = "0.5.11"
VALID_LOG_LEVELS = {"trace", "debug", "info", "notice", "warning", "error", "fatal"}
DEFAULT_OPTIONS = {
    "log_level": "info",
    "database_path": "/homeassistant_config/home-assistant_v2.db",
    "max_upload_mb": 131072,
    "create_current_db_backup": True,
    "cache_path": str(DATA_DIR / "cache"),
    "config_backup_path": str(DATA_DIR / "config-backups"),
}


def read_startup_options() -> dict[str, Any]:
    options = DEFAULT_OPTIONS.copy()
    if OPTIONS_PATH.exists():
        try:
            with OPTIONS_PATH.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                options.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    return options


def configured_cache_dir(options: dict[str, Any]) -> Path:
    configured = os.environ.get("BACKUP_DB_RESTORE_CACHE_PATH") or options.get("cache_path") or DEFAULT_OPTIONS["cache_path"]
    configured_text = str(configured).strip() or DEFAULT_OPTIONS["cache_path"]
    cache_path = Path(configured_text).expanduser()
    if not cache_path.is_absolute():
        cache_path = DATA_DIR / cache_path
    return cache_path


def configured_config_backup_dir(options: dict[str, Any] | None = None) -> Path:
    source_options = options or read_startup_options()
    configured = (
        os.environ.get("BACKUP_DB_RESTORE_CONFIG_BACKUP_PATH")
        or source_options.get("config_backup_path")
        or DEFAULT_OPTIONS["config_backup_path"]
    )
    configured_text = str(configured).strip() or DEFAULT_OPTIONS["config_backup_path"]
    backup_path = Path(configured_text).expanduser()
    if not backup_path.is_absolute():
        backup_path = DATA_DIR / backup_path
    return backup_path


STARTUP_OPTIONS = read_startup_options()
CACHE_DIR = configured_cache_dir(STARTUP_OPTIONS)
UPLOAD_DIR = CACHE_DIR / "uploads"
TMP_DIR = CACHE_DIR / "tmp"
CURRENT_DB_BACKUP_DIR = DATA_DIR / "current-db-backups"
REPORT_DIR = DATA_DIR / "import-reports"
DIAGNOSTIC_DIR = DATA_DIR / "diagnostics"

JOBS_PATH = DATA_DIR / "jobs.json"
SOURCE_DB = CACHE_DIR / "source.db"
SOURCE_ORIGINAL = CACHE_DIR / "source_original"
SOURCE_META = CACHE_DIR / "source_meta.json"
STATISTICS_TABLES = ("statistics_short_term", "statistics")
BACKUP_FILE_EXTENSIONS = (".backup", ".tar", ".tar.gz", ".tgz", ".db", ".sqlite", ".sqlite3")
CONFIG_BACKUP_EXTENSION = ".tar.gz"
CONFIG_BACKUP_COMPONENTS: dict[str, dict[str, Any]] = {
    "automations": {"label": "Automationen", "patterns": ["automations.yaml", ".storage/automation"]},
    "scripts": {"label": "Skripte", "patterns": ["scripts.yaml", ".storage/script"]},
    "scenes": {"label": "Szenen", "patterns": ["scenes.yaml", ".storage/scene"]},
    "blueprints": {"label": "Blueprints", "patterns": ["blueprints"]},
    "dashboards": {"label": "Dashboards", "patterns": [".storage/lovelace", ".storage/lovelace.*"]},
    "helpers": {
        "label": "Helpers und Registries",
        "patterns": [
            ".storage/core.area_registry",
            ".storage/core.device_registry",
            ".storage/core.entity_registry",
            ".storage/core.floor_registry",
            ".storage/core.label_registry",
            ".storage/counter",
            ".storage/group",
            ".storage/input_*",
            ".storage/person",
            ".storage/schedule",
            ".storage/timer",
            ".storage/zone",
        ],
    },
    "configuration": {
        "label": "Konfiguration und Pakete",
        "patterns": ["configuration.yaml", "customize.yaml", "packages", "custom_templates"],
    },
    "secrets": {"label": "Secrets", "patterns": ["secrets.yaml"], "sensitive": True},
}
DEFAULT_CONFIG_BACKUP_COMPONENTS = ("automations", "scripts", "scenes", "blueprints", "dashboards", "helpers")
JOB_LOG_LIMIT = 300
JOB_RETENTION_SECONDS = 6 * 60 * 60
ANALYSIS_CACHE_TTL_SECONDS = 5 * 60
AUTOMATIC_DEEP_ANALYSIS_MAX_BYTES = 2 * 1024 * 1024 * 1024
ACTIVE_JOB_STATUSES = {"queued", "running", "cancelling"}
TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
SOURCE_CACHE_JOB_KINDS = {"upload", "load_backup", "load_corrupt_database", "refresh_cache"}
SOURCE_READER_JOB_KINDS = {"import"}
CURRENT_DB_WRITER_JOB_KINDS = {
    "import",
    "restore_current_db",
    "checkpoint_current_db",
    "purge_entity_history",
}

ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
JOB_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}
ANALYSIS_CACHE_LOCK = threading.Lock()
ANALYSIS_CACHE: dict[str, dict[str, Any]] = {}


class AppError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details or {}


class JobCancelled(Exception):
    pass


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "title": job["title"],
        "status": job["status"],
        "progress": job["progress"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "finished_at": job.get("finished_at"),
        "cancel_requested": bool(job.get("cancel_requested", False)),
        "logs": list(job["logs"]),
        "result": job.get("result"),
        "error": job.get("error"),
    }


def app_error_payload(err: AppError) -> dict[str, Any]:
    payload = {"error": err.message}
    payload.update(err.details)
    return payload


def compact_job_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    compact = dict(value)
    entities = compact.get("entities")
    if isinstance(entities, list):
        compact["entities_count"] = len(entities)
        compact["entities_omitted"] = True
        compact.pop("entities", None)
    return compact


def persisted_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "title": job["title"],
        "status": job["status"],
        "progress": job["progress"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "finished_at": job.get("finished_at"),
        "cancel_requested": bool(job.get("cancel_requested", False)),
        "logs": list(job.get("logs", []))[-JOB_LOG_LIMIT:],
        "result": job.get("result"),
        "error": job.get("error"),
    }


def persist_jobs_locked() -> None:
    payload = {
        "version": 1,
        "updated_at": now_iso(),
        "jobs": [persisted_job(job) for job in sorted(JOBS.values(), key=lambda item: item["created_at"])],
    }
    temp_path: Path | None = None
    try:
        JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = JOBS_PATH.with_name(f".{JOBS_PATH.name}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=json_default)
        temp_path.replace(JOBS_PATH)
    except (OSError, TypeError, ValueError):
        if temp_path:
            try:
                temp_path.unlink()
            except OSError:
                pass


def load_persisted_jobs() -> None:
    if not JOBS_PATH.exists():
        return
    try:
        with JOBS_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return

    raw_jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_jobs, list):
        return

    loaded: dict[str, dict[str, Any]] = {}
    restart_time = now_iso()
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict) or not raw_job.get("id"):
            continue
        job = {
            "id": str(raw_job.get("id")),
            "kind": str(raw_job.get("kind") or "unknown"),
            "title": str(raw_job.get("title") or "Job"),
            "status": str(raw_job.get("status") or "failed"),
            "progress": max(0, min(100, int(raw_job.get("progress") or 0))),
            "created_at": str(raw_job.get("created_at") or restart_time),
            "updated_at": str(raw_job.get("updated_at") or restart_time),
            "finished_at": raw_job.get("finished_at"),
            "cancel_requested": bool(raw_job.get("cancel_requested", False)),
            "logs": list(raw_job.get("logs") or [])[-JOB_LOG_LIMIT:],
            "result": compact_job_result(raw_job.get("result")),
            "error": raw_job.get("error"),
        }
        if job["status"] in ACTIVE_JOB_STATUSES:
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["status"] = "failed"
            job["progress"] = 100
            job["cancel_requested"] = False
            job["error"] = "Job wurde durch einen Server-Neustart unterbrochen."
            job["updated_at"] = restart_time
            job["finished_at"] = restart_time
            job["logs"].append(f"[{timestamp}] Server-Neustart erkannt. Job als unterbrochen markiert.")
        if job["status"] in TERMINAL_JOB_STATUSES:
            job["finished_monotonic"] = time.time()
        loaded[job["id"]] = job

    with JOB_LOCK:
        JOBS.update(loaded)
        persist_jobs_locked()


def cleanup_jobs() -> None:
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with JOB_LOCK:
        stale = [
            job_id
            for job_id, job in JOBS.items()
            if job["status"] in TERMINAL_JOB_STATUSES and job.get("finished_monotonic", time.time()) < cutoff
        ]
        for job_id in stale:
            JOBS.pop(job_id, None)
        if stale:
            persist_jobs_locked()


def get_job(job_id: str) -> dict[str, Any]:
    cleanup_jobs()
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise AppError("Job was not found.", HTTPStatus.NOT_FOUND)
        return public_job(job)


def active_job() -> dict[str, Any] | None:
    cleanup_jobs()
    with JOB_LOCK:
        candidates = [job for job in JOBS.values() if job["status"] in ACTIVE_JOB_STATUSES]
        if not candidates:
            return None
        newest = max(candidates, key=lambda item: item["updated_at"])
        return public_job(newest)


def conflicting_job_locked(kind: str) -> dict[str, Any] | None:
    conflicting_kinds: set[str] = set()
    if kind in SOURCE_CACHE_JOB_KINDS:
        conflicting_kinds.update(SOURCE_CACHE_JOB_KINDS | SOURCE_READER_JOB_KINDS)
    elif kind in SOURCE_READER_JOB_KINDS:
        conflicting_kinds.update(SOURCE_CACHE_JOB_KINDS)
    if kind in CURRENT_DB_WRITER_JOB_KINDS:
        conflicting_kinds.update(CURRENT_DB_WRITER_JOB_KINDS)
    if not conflicting_kinds:
        return None
    for job in JOBS.values():
        if job["kind"] in conflicting_kinds and job["status"] in ACTIVE_JOB_STATUSES:
            return public_job(job)
    return None


def raise_job_conflict(conflict: dict[str, Any]) -> None:
    raise AppError(
        "Es laeuft bereits ein Job, der dieselben Datenbankdateien verwendet.",
        HTTPStatus.CONFLICT,
        {"active_job": conflict},
    )


def ensure_no_conflicting_job(kind: str) -> None:
    cleanup_jobs()
    with JOB_LOCK:
        conflict = conflicting_job_locked(kind)
        if conflict:
            raise_job_conflict(conflict)


def raise_if_cancelled(job_id: str) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job and job.get("cancel_requested"):
            if job["status"] in {"queued", "running"}:
                job["status"] = "cancelling"
                job["updated_at"] = now_iso()
                persist_jobs_locked()
            raise JobCancelled()


def request_job_cancel(job_id: str) -> dict[str, Any]:
    cleanup_jobs()
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise AppError("Job was not found.", HTTPStatus.NOT_FOUND)
        if job["status"] in TERMINAL_JOB_STATUSES:
            return public_job(job)
        job["cancel_requested"] = True
        if job["status"] in {"queued", "running"}:
            job["status"] = "cancelling"
        timestamp = datetime.now().strftime("%H:%M:%S")
        if not job["logs"] or "Abbruch angefordert." not in job["logs"][-1]:
            job["logs"].append(f"[{timestamp}] Abbruch angefordert.")
        job["updated_at"] = now_iso()
        persist_jobs_locked()
        return public_job(job)


def update_job(job_id: str, *, progress: int | None = None, status: str | None = None, message: str | None = None) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        if progress is not None:
            job["progress"] = max(0, min(100, int(progress)))
        if status is not None:
            job["status"] = status
        if message:
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] {message}")
            if len(job["logs"]) > JOB_LOG_LIMIT:
                job["logs"] = job["logs"][-JOB_LOG_LIMIT:]
        job["updated_at"] = now_iso()
        persist_jobs_locked()


def start_job(kind: str, title: str, worker: Any, *args: Any) -> dict[str, Any]:
    cleanup_jobs()
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "kind": kind,
        "title": title,
        "status": "queued",
        "progress": 0,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "cancel_requested": False,
        "logs": [],
        "result": None,
        "error": None,
    }
    with JOB_LOCK:
        conflict = conflicting_job_locked(kind)
        if conflict:
            raise_job_conflict(conflict)
        JOBS[job_id] = job
        persist_jobs_locked()

    thread = threading.Thread(target=run_job, args=(job_id, worker, args), daemon=True)
    thread.start()
    return get_job(job_id)


def run_job(job_id: str, worker: Any, args: tuple[Any, ...]) -> None:
    update_job(job_id, status="running", progress=1, message="Job gestartet.")
    try:
        raise_if_cancelled(job_id)
        result = worker(job_id, *args)
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "succeeded"
            job["progress"] = 100
            job["result"] = compact_job_result(result)
            job["updated_at"] = now_iso()
            job["finished_at"] = now_iso()
            job["finished_monotonic"] = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] Job abgeschlossen.")
            persist_jobs_locked()
    except JobCancelled:
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "cancelled"
            job["progress"] = 100
            job["updated_at"] = now_iso()
            job["finished_at"] = now_iso()
            job["finished_monotonic"] = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] Job abgebrochen.")
            persist_jobs_locked()
    except AppError as err:
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["progress"] = 100
            job["error"] = err.message
            job["updated_at"] = now_iso()
            job["finished_at"] = now_iso()
            job["finished_monotonic"] = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] Fehler: {err.message}")
            persist_jobs_locked()
    except Exception as err:
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["progress"] = 100
            job["error"] = str(err)
            job["updated_at"] = now_iso()
            job["finished_at"] = now_iso()
            job["finished_monotonic"] = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] Fehler: {err}")
            persist_jobs_locked()


def ensure_dirs() -> None:
    if not CACHE_DIR.parent.exists():
        raise RuntimeError(f"Cache parent directory does not exist: {CACHE_DIR.parent}")
    for path in (CACHE_DIR, UPLOAD_DIR, TMP_DIR, CURRENT_DB_BACKUP_DIR, REPORT_DIR, DIAGNOSTIC_DIR):
        path.mkdir(parents=True, exist_ok=True)
    for path in (CACHE_DIR, UPLOAD_DIR, TMP_DIR):
        if not os.access(path, os.R_OK | os.W_OK):
            raise RuntimeError(f"Cache directory is not readable and writable: {path}")


def read_options() -> dict[str, Any]:
    options = DEFAULT_OPTIONS.copy()
    if OPTIONS_PATH.exists():
        try:
            with OPTIONS_PATH.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                options.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    return options


def write_options(options: dict[str, Any]) -> None:
    OPTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = OPTIONS_PATH.with_name(f".{OPTIONS_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(options, handle, indent=2, sort_keys=True, default=json_default)
        temp_path.replace(OPTIONS_PATH)
    except OSError as err:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise AppError(f"Settings could not be saved: {err}") from err


def normalized_path_setting(value: Any, default: str, *, relative_to_data: bool = False) -> str:
    text = str(value or "").strip() or default
    path = Path(text).expanduser()
    if relative_to_data and not path.is_absolute():
        path = DATA_DIR / path
    return str(path)


def normalize_settings_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise AppError("Settings payload needs to be an object.")
    current = read_options()
    updated = current.copy()
    updated["log_level"] = str(payload.get("log_level", current.get("log_level", "info"))).strip() or "info"
    if updated["log_level"] not in VALID_LOG_LEVELS:
        raise AppError("Unsupported log level.")

    updated["database_path"] = normalized_path_setting(
        payload.get("database_path", current.get("database_path")),
        DEFAULT_OPTIONS["database_path"],
    )
    updated["cache_path"] = normalized_path_setting(
        payload.get("cache_path", current.get("cache_path")),
        DEFAULT_OPTIONS["cache_path"],
        relative_to_data=True,
    )
    updated["config_backup_path"] = normalized_path_setting(
        payload.get("config_backup_path", current.get("config_backup_path")),
        DEFAULT_OPTIONS["config_backup_path"],
        relative_to_data=True,
    )

    try:
        updated["max_upload_mb"] = max(1, min(131072, int(payload.get("max_upload_mb", current.get("max_upload_mb", 131072)))))
    except (TypeError, ValueError) as err:
        raise AppError("Maximum upload size needs to be a number.") from err
    updated["create_current_db_backup"] = bool(payload.get("create_current_db_backup", current.get("create_current_db_backup", True)))

    validation = {
        "database_path": file_info(Path(str(updated["database_path"]))),
        "cache_path": storage_info(Path(str(updated["cache_path"]))),
        "config_backup_path": storage_info(Path(str(updated["config_backup_path"]))),
    }
    for key in ("cache_path", "config_backup_path"):
        configured = Path(str(updated[key]))
        if not configured.parent.exists():
            raise AppError(f"{key} parent directory does not exist: {configured.parent}")
    return updated, validation


def settings_status(options: dict[str, Any] | None = None) -> dict[str, Any]:
    current = options or read_options()
    configured_cache = configured_cache_dir(current)
    configured_config_backup = configured_config_backup_dir(current)
    restart_required = []
    if configured_cache != CACHE_DIR:
        restart_required.append("cache_path")
    if str(current.get("log_level", "info")) != str(STARTUP_OPTIONS.get("log_level", "info")):
        restart_required.append("log_level")
    return {
        "options": {
            "log_level": str(current.get("log_level", "info")),
            "database_path": str(current.get("database_path", DEFAULT_OPTIONS["database_path"])),
            "cache_path": str(configured_cache),
            "config_backup_path": str(configured_config_backup),
            "max_upload_mb": int(current.get("max_upload_mb", 131072)),
            "create_current_db_backup": bool(current.get("create_current_db_backup", True)),
        },
        "effective": {
            "cache_path": str(CACHE_DIR),
            "upload_dir": str(UPLOAD_DIR),
            "tmp_dir": str(TMP_DIR),
            "config_backup_path": str(config_backup_dir(current)),
        },
        "storage": {
            "cache_path": storage_info(CACHE_DIR),
            "configured_cache_path": storage_info(configured_cache),
            "config_backup_path": storage_info(config_backup_dir(current)),
        },
        "restart_required": restart_required,
    }


def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if active_job():
        raise AppError("Settings cannot be changed while a job is running.", HTTPStatus.CONFLICT)
    updated, validation = normalize_settings_payload(payload)
    write_options(updated)
    status = settings_status(updated)
    status["validation"] = validation
    status["saved_at"] = now_iso()
    return status


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError as err:
            raise AppError(f"Invalid datetime value: {value}") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def datetime_for_column(value: Any, column: str) -> Any:
    parsed = parse_datetime_value(value)
    if parsed is None:
        return None
    return datetime_bound_for_column(parsed, column)


def datetime_bound_for_column(parsed: datetime, column: str) -> Any:
    if column.endswith("_ts"):
        return parsed.timestamp()
    return parsed.isoformat().replace("+00:00", "Z")


def format_db_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError):
            return str(value)
    text = str(value)
    if not text:
        return None
    return text


def safe_artifact_id(value: str) -> str:
    normalized = Path(value).name
    if not re.match(r"^[A-Za-z0-9_.-]+$", normalized):
        raise AppError("Invalid artifact id.")
    return normalized


def sqlite_uri(path: Path, readonly: bool = False, immutable: bool = False) -> str:
    quoted = urllib.parse.quote(str(path), safe="/:")
    params: list[str] = []
    if readonly:
        params.append("mode=ro")
    if immutable:
        params.append("immutable=1")
    query = f"?{'&'.join(params)}" if params else ""
    return f"file:{quoted}{query}"


def open_db(path: Path, readonly: bool = False, timeout: float = 30.0, immutable: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(sqlite_uri(path, readonly=True, immutable=immutable), uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def open_db_readonly_resilient(path: Path, timeout: float = 30.0) -> tuple[sqlite3.Connection, str | None]:
    try:
        return open_db(path, readonly=True, timeout=timeout), None
    except sqlite3.DatabaseError as err:
        conn = open_db(path, readonly=True, timeout=timeout, immutable=True)
        return conn, f"readonly_open: {err}; immutable fallback active"


def is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def file_info(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as err:
        return {"path": str(path), "exists": False, "error": str(err)}
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
    }


def storage_info(path: Path) -> dict[str, Any]:
    target = path if path.exists() else path.parent
    result = {
        "path": str(path),
        "exists": path.exists(),
        "readable": os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
        "free_bytes": None,
        "total_bytes": None,
        "used_bytes": None,
        "error": None,
    }
    try:
        usage = shutil.disk_usage(target)
        result.update(
            {
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
            }
        )
    except OSError as err:
        result["error"] = str(err)
    return result


def database_sidecar_files(path: Path) -> dict[str, dict[str, Any]]:
    return {
        "wal": file_info(Path(f"{path}-wal")),
        "shm": file_info(Path(f"{path}-shm")),
        "journal": file_info(Path(f"{path}-journal")),
    }


def analysis_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def remember_analysis(path: Path, analysis: dict[str, Any]) -> None:
    signature = analysis_signature(path)
    if signature is None:
        return
    cached = copy.deepcopy(analysis)
    cached["path"] = str(path)
    cached["exists"] = path.exists()
    cached["size_bytes"] = signature[0]
    cached["sidecars"] = database_sidecar_files(path)
    with ANALYSIS_CACHE_LOCK:
        ANALYSIS_CACHE[str(path)] = {
            "signature": signature,
            "cached_monotonic": time.time(),
            "analysis": cached,
        }


def cached_analyze_database(path: Path, ttl_seconds: int = ANALYSIS_CACHE_TTL_SECONDS) -> dict[str, Any]:
    signature = analysis_signature(path)
    cache_key = str(path)
    now = time.time()
    if signature is not None:
        with ANALYSIS_CACHE_LOCK:
            cached = ANALYSIS_CACHE.get(cache_key)
            if (
                cached
                and cached.get("signature") == signature
                and now - float(cached.get("cached_monotonic", 0)) <= ttl_seconds
            ):
                return copy.deepcopy(cached["analysis"])

    analysis = analyze_database(path)
    remember_analysis(path, analysis)
    return copy.deepcopy(analysis)


def lightweight_database_status(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "sqlite_header": False,
        "readable": False,
        "partial": False,
        "ok": False,
        "integrity": [],
        "foreign_key_errors": [],
        "tables": [],
        "states_count": 0,
        "entities_count": 0,
        "statistics_count": 0,
        "statistics_short_term_count": 0,
        "statistics_entities_count": 0,
        "first_statistic": None,
        "last_statistic": None,
        "first_state": None,
        "last_state": None,
        "quick_check": [],
        "journal_mode": None,
        "page_count": None,
        "freelist_count": None,
        "schema_version": None,
        "user_version": None,
        "sidecars": database_sidecar_files(path),
        "read_errors": [],
        "error": None,
    }
    if not path.exists():
        result["error"] = "Database file does not exist."
    elif not is_sqlite_file(path):
        result["error"] = "File is not a SQLite database."
    else:
        result["sqlite_header"] = True
        conn: sqlite3.Connection | None = None
        try:
            conn, fallback_warning = open_db_readonly_resilient(path, timeout=2)
            if fallback_warning:
                result["read_errors"].append(fallback_warning)
                result["partial"] = True
            result["readable"] = True
            for pragma in ("journal_mode", "page_count", "freelist_count", "schema_version", "user_version"):
                try:
                    result[pragma] = read_pragma(conn, pragma)
                except sqlite3.DatabaseError as err:
                    result["read_errors"].append(read_error(pragma, err))
                    result["partial"] = True
            try:
                tables = sorted(table_names(conn))
                result["tables"] = tables
                entity_selects: list[str] = []
                if "states_meta" in tables and "entity_id" in column_names(conn, "states_meta"):
                    entity_selects.append("SELECT entity_id FROM states_meta WHERE entity_id IS NOT NULL")
                if "statistics_meta" in tables and "statistic_id" in column_names(conn, "statistics_meta"):
                    entity_selects.append(
                        "SELECT statistic_id AS entity_id FROM statistics_meta WHERE statistic_id IS NOT NULL AND instr(statistic_id, '.') > 0"
                    )
                if entity_selects:
                    union_sql = " UNION ".join(entity_selects)
                    result["entities_count"] = int(conn.execute(f"SELECT COUNT(*) FROM ({union_sql}) entity_ids").fetchone()[0])
                    result["statistics_entities_count"] = result["entities_count"]
            except sqlite3.DatabaseError as err:
                result["read_errors"].append(read_error("table_summary", err))
                result["partial"] = True
        except sqlite3.DatabaseError as err:
            result["error"] = str(err)
            result["partial"] = True
        finally:
            if conn is not None:
                conn.close()
    result["diagnostics"] = build_database_diagnostics(result)
    return result


def analyze_database_for_cache(path: Path) -> dict[str, Any]:
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0
    if size_bytes <= AUTOMATIC_DEEP_ANALYSIS_MAX_BYTES:
        analysis = analyze_database(path)
        analysis["analysis_mode"] = "full"
        return analysis

    analysis = lightweight_database_status(path)
    analysis["analysis_mode"] = "quick_large_database"
    if analysis.get("readable"):
        analysis["ok"] = True
        analysis["error"] = None
        diagnostics = build_database_diagnostics(analysis)
        diagnostics.setdefault("recommendations", []).insert(
            0,
            {
                "title": "Schnellanalyse fuer grosse DB",
                "detail": "Automatische Vollpruefungen wurden uebersprungen, damit Home Assistant und andere Add-ons nicht durch 33-GB-Scans blockieren.",
            },
        )
        analysis["diagnostics"] = diagnostics
    return analysis


def source_cache_analysis(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not SOURCE_DB.exists():
        return None
    if isinstance(meta, dict) and isinstance(meta.get("analysis"), dict):
        analysis = copy.deepcopy(meta["analysis"])
        analysis["path"] = str(SOURCE_DB)
        analysis["exists"] = True
        try:
            analysis["size_bytes"] = SOURCE_DB.stat().st_size
        except OSError:
            pass
        analysis["sidecars"] = database_sidecar_files(SOURCE_DB)
        analysis["diagnostics"] = build_database_diagnostics(analysis)
        remember_analysis(SOURCE_DB, analysis)
        return analysis
    return lightweight_database_status(SOURCE_DB)


def read_pragma(conn: sqlite3.Connection, pragma: str) -> Any:
    row = conn.execute(f"PRAGMA {pragma}").fetchone()
    if row is None:
        return None
    if len(row.keys()) == 1:
        return row[0]
    return dict(row)


def build_database_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    problems: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    actions: list[str] = ["reanalyze", "snapshot"]
    sidecars = result.get("sidecars") or {}
    wal = sidecars.get("wal") or {}
    shm = sidecars.get("shm") or {}

    def add_problem(severity: str, title: str, detail: str) -> None:
        problems.append({"severity": severity, "title": title, "detail": detail})

    def add_recommendation(title: str, detail: str, action: str | None = None) -> None:
        item = {"title": title, "detail": detail}
        if action:
            item["action"] = action
        recommendations.append(item)

    if not result.get("exists"):
        add_problem("error", "Datei nicht gefunden", "Der konfigurierte Datenbankpfad existiert nicht.")
        add_recommendation("Pfad pruefen", "Pruefe die Add-on-Option database_path und den Mount /homeassistant_config.")
        return {"problems": problems, "recommendations": recommendations, "actions": actions}

    if not result.get("sqlite_header"):
        add_problem("error", "Keine SQLite-Datei", "Die Datei hat keinen gueltigen SQLite-Header.")
        add_recommendation("Richtige Recorder-DB waehlen", "Der Pfad sollte normalerweise auf home-assistant_v2.db zeigen.")
        return {"problems": problems, "recommendations": recommendations, "actions": actions}

    if not result.get("readable"):
        add_problem("error", "SQLite nicht lesbar", result.get("error") or "Die Datei konnte nicht als SQLite-Datenbank geoeffnet werden.")
        add_recommendation("Berechtigungen und Locks pruefen", "Pruefe, ob Home Assistant oder das Betriebssystem den Zugriff blockiert.")

    integrity = [entry for entry in result.get("integrity", []) if entry != "ok"]
    for entry in integrity[:8]:
        add_problem("error", "Integrity-Check meldet Fehler", str(entry))
    if len(integrity) > 8:
        add_problem("error", "Weitere Integrity-Fehler", f"{len(integrity) - 8} weitere Meldung(en) wurden gekuerzt.")

    if result.get("read_errors"):
        for entry in result["read_errors"][:8]:
            add_problem("warning", "Lesewarnung", str(entry))

    if result.get("foreign_key_errors"):
        add_problem("warning", "Foreign-Key-Warnungen", f"{len(result['foreign_key_errors'])} Foreign-Key-Problem(e) gefunden.")

    tables = set(result.get("tables") or [])
    if result.get("readable") and "states" not in tables:
        add_problem("warning", "States-Tabelle fehlt", "Die Recorder-Tabelle states wurde nicht gefunden.")
    if result.get("readable") and result.get("states_count", 0) == 0:
        add_problem("warning", "Keine State-History gefunden", "Die Datenbank ist lesbar, enthaelt aber keine State-Zeilen.")

    if wal.get("exists") and wal.get("size_bytes", 0) > 0:
        add_problem("info", "WAL-Datei vorhanden", f"{wal.get('size_bytes')} Byte liegen in der Write-Ahead-Log-Datei.")
        if not shm.get("exists"):
            add_problem("warning", "SHM-Datei fehlt", "Zur WAL-Datei wurde keine passende -shm-Datei gefunden.")
        add_recommendation(
            "Passiven WAL-Checkpoint versuchen",
            "Wenn die DB nur wegen ausstehender WAL-Daten auffaellig ist, kann ein passiver Checkpoint helfen. Das blockiert Schreibzugriffe nicht aggressiv.",
            "checkpoint",
        )
        if "checkpoint" not in actions:
            actions.append("checkpoint")

    if result.get("ok"):
        add_recommendation("Keine Reparatur noetig", "Der SQLite-Integrity-Check ist ok.")
    else:
        add_recommendation(
            "Konsistente Sicherung/Snapshot erstellen",
            "Erstellt mit SQLite Backup API eine neue Sicherung der aktuellen DB und analysiert diese separat.",
            "snapshot",
        )
        add_recommendation(
            "Home Assistant neu starten",
            "Nach externen DB-Aktionen oder bei Recorder-Locks kann ein Neustart den Recorder-Zustand bereinigen.",
        )
        add_recommendation(
            "Aus Home-Assistant-Backup wiederherstellen",
            "Wenn der Integrity-Check echte Korruption meldet, ist ein Restore aus einem bekannten guten HA-Backup meist die sicherste Loesung.",
        )

    return {"problems": problems, "recommendations": recommendations, "actions": actions}


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def table_columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchall()
    return [dict(row) for row in rows]


def column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(column["name"]) for column in table_columns(conn, table)]


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def state_time_column_name(columns: list[str]) -> str | None:
    for column in ("last_updated_ts", "last_changed_ts", "last_updated", "last_changed"):
        if column in columns:
            return column
    return None


def state_time_column(columns: list[str], alias: str | None = None) -> str | None:
    prefix = f"{alias}." if alias else ""
    column = state_time_column_name(columns)
    return f"{prefix}{quote_identifier(column)}" if column else None


def state_order_clause(columns: list[str], alias: str = "s") -> str:
    parts: list[str] = []
    for column in ("last_updated_ts", "last_changed_ts", "last_updated", "last_changed", "state_id"):
        if column in columns:
            parts.append(f"{alias}.{quote_identifier(column)}")
    return ", ".join(parts) if parts else "1"


def start_time_column(columns: list[str]) -> str | None:
    for column in ("start_ts", "start"):
        if column in columns:
            return column
    return None


def source_entity_join_and_where(conn: sqlite3.Connection, entity_id: str) -> tuple[str, str, list[Any]]:
    tables = table_names(conn)
    state_columns = column_names(conn, "states")
    if "states_meta" in tables and "metadata_id" in state_columns:
        return (
            "JOIN states_meta sm ON sm.metadata_id = s.metadata_id",
            "sm.entity_id = ?",
            [entity_id],
        )
    if "entity_id" in state_columns:
        return "", "s.entity_id = ?", [entity_id]
    raise AppError("The source database has no supported entity mapping in the states table.")


def analyze_database(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "sqlite_header": False,
        "ok": False,
        "integrity": [],
        "foreign_key_errors": [],
        "tables": [],
        "states_count": 0,
        "entities_count": 0,
        "statistics_count": 0,
        "statistics_short_term_count": 0,
        "statistics_entities_count": 0,
        "first_statistic": None,
        "last_statistic": None,
        "first_state": None,
        "last_state": None,
        "error": None,
    }

    if not path.exists():
        result["error"] = "Database file does not exist."
        result["diagnostics"] = build_database_diagnostics(result)
        return result
    if not is_sqlite_file(path):
        result["error"] = "File is not a SQLite database."
        result["diagnostics"] = build_database_diagnostics(result)
        return result

    result["sqlite_header"] = True
    try:
        with open_db(path, readonly=True) as conn:
            integrity_rows = conn.execute("PRAGMA integrity_check(20)").fetchall()
            result["integrity"] = [str(row[0]) for row in integrity_rows]
            result["ok"] = result["integrity"] == ["ok"]

            try:
                fk_rows = conn.execute("PRAGMA foreign_key_check").fetchmany(20)
                result["foreign_key_errors"] = [list(row) for row in fk_rows]
            except sqlite3.DatabaseError:
                result["foreign_key_errors"] = []

            tables = sorted(table_names(conn))
            result["tables"] = tables
            if "states" in tables:
                state_columns = column_names(conn, "states")
                result["states_count"] = int(conn.execute("SELECT COUNT(*) FROM states").fetchone()[0])
                time_expr = state_time_column(state_columns)
                if time_expr:
                    row = conn.execute(f"SELECT MIN({time_expr}), MAX({time_expr}) FROM states").fetchone()
                    result["first_state"] = format_db_time(row[0])
                    result["last_state"] = format_db_time(row[1])
                result["entities_count"] = len(list_entities(path, limit=None))
            if "statistics_meta" in tables:
                result["statistics_count"] = int(conn.execute("SELECT COUNT(*) FROM statistics_meta").fetchone()[0])
                result["statistics_entities_count"] = len(list_statistics(path, limit=None))
            for table in STATISTICS_TABLES:
                if table not in tables:
                    continue
                columns = column_names(conn, table)
                count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0])
                if table == "statistics_short_term":
                    result["statistics_short_term_count"] = count
                else:
                    result["statistics_count"] = count
                start_column = start_time_column(columns)
                if start_column:
                    row = conn.execute(
                        f"SELECT MIN({quote_identifier(start_column)}), MAX({quote_identifier(start_column)}) FROM {quote_identifier(table)}"
                    ).fetchone()
                    if row[0] is not None:
                        first = format_db_time(row[0])
                        last = format_db_time(row[1])
                        if result["first_statistic"] is None or str(first) < str(result["first_statistic"]):
                            result["first_statistic"] = first
                        if result["last_statistic"] is None or str(last) > str(result["last_statistic"]):
                            result["last_statistic"] = last
    except sqlite3.DatabaseError as err:
        result["error"] = str(err)
        result["ok"] = False
    return result


def statistics_meta_primary_key(columns: list[dict[str, Any]]) -> str | None:
    for column in columns:
        if column.get("pk"):
            return str(column["name"])
    return "id" if any(column["name"] == "id" for column in columns) else None


def table_primary_key(columns: list[dict[str, Any]], fallback: str = "id") -> str | None:
    for column in columns:
        if column.get("pk"):
            return str(column["name"])
    return fallback if any(column["name"] == fallback for column in columns) else None


def list_statistics(path: Path, limit: int | None = 5000) -> list[dict[str, Any]]:
    if not path.exists() or not is_sqlite_file(path):
        return []

    with open_db(path, readonly=True) as conn:
        tables = table_names(conn)
        if "statistics_meta" not in tables:
            return []
        meta_columns = table_columns(conn, "statistics_meta")
        meta_names = [str(column["name"]) for column in meta_columns]
        meta_pk = statistics_meta_primary_key(meta_columns)
        if not meta_pk or "statistic_id" not in meta_names:
            return []

        summaries: dict[str, dict[str, Any]] = {}
        for table in STATISTICS_TABLES:
            if table not in tables:
                continue
            columns = column_names(conn, table)
            if "metadata_id" not in columns:
                continue
            start_column = start_time_column(columns)
            first_expr = f"MIN(t.{quote_identifier(start_column)})" if start_column else "NULL"
            last_expr = f"MAX(t.{quote_identifier(start_column)})" if start_column else "NULL"
            sql = f"""
                SELECT
                    sm.statistic_id AS statistic_id,
                    COUNT(*) AS row_count,
                    {first_expr} AS first_seen,
                    {last_expr} AS last_seen
                FROM {quote_identifier(table)} t
                JOIN statistics_meta sm ON sm.{quote_identifier(meta_pk)} = t.metadata_id
                GROUP BY sm.statistic_id
                ORDER BY sm.statistic_id
            """
            params: list[Any] = []
            if limit is not None:
                sql = f"{sql} LIMIT ?"
                params.append(limit)

            for row in conn.execute(sql, params):
                statistic_id = row["statistic_id"]
                summary = summaries.setdefault(
                    statistic_id,
                    {
                        "statistic_id": statistic_id,
                        "statistics_count": 0,
                        "statistics_short_term_count": 0,
                        "first_statistic": None,
                        "last_statistic": None,
                    },
                )
                count_key = "statistics_short_term_count" if table == "statistics_short_term" else "statistics_count"
                summary[count_key] = int(row["row_count"])
                first_seen = format_db_time(row["first_seen"])
                last_seen = format_db_time(row["last_seen"])
                if first_seen and (summary["first_statistic"] is None or str(first_seen) < str(summary["first_statistic"])):
                    summary["first_statistic"] = first_seen
                if last_seen and (summary["last_statistic"] is None or str(last_seen) > str(summary["last_statistic"])):
                    summary["last_statistic"] = last_seen

        return sorted(summaries.values(), key=lambda item: item["statistic_id"])[: limit or None]


def list_entities(path: Path, limit: int | None = 5000) -> list[dict[str, Any]]:
    if not path.exists() or not is_sqlite_file(path):
        return []

    with open_db(path, readonly=True) as conn:
        tables = table_names(conn)
        entities_by_id: dict[str, dict[str, Any]] = {}
        if "states" in tables:
            state_columns = column_names(conn, "states")
            time_expr = state_time_column(state_columns, alias="s")
            first_expr = f"MIN({time_expr})" if time_expr else "NULL"
            last_expr = f"MAX({time_expr})" if time_expr else "NULL"

            if "states_meta" in tables and "metadata_id" in state_columns:
                sql = f"""
                    SELECT
                        sm.entity_id AS entity_id,
                        COUNT(*) AS states_count,
                        {first_expr} AS first_seen,
                        {last_expr} AS last_seen
                    FROM states s
                    JOIN states_meta sm ON sm.metadata_id = s.metadata_id
                    GROUP BY sm.entity_id
                    ORDER BY sm.entity_id
                """
                params: list[Any] = []
            elif "entity_id" in state_columns:
                sql = f"""
                    SELECT
                        s.entity_id AS entity_id,
                        COUNT(*) AS states_count,
                        {first_expr} AS first_seen,
                        {last_expr} AS last_seen
                    FROM states s
                    WHERE s.entity_id IS NOT NULL
                    GROUP BY s.entity_id
                    ORDER BY s.entity_id
                """
                params = []
            else:
                sql = ""
                params = []

            if sql:
                if limit is not None:
                    sql = f"{sql} LIMIT ?"
                    params.append(limit)

                for row in conn.execute(sql, params):
                    entity_id = row["entity_id"]
                    entities_by_id[entity_id] = {
                        "entity_id": entity_id,
                        "states_count": int(row["states_count"]),
                        "statistics_count": 0,
                        "statistics_short_term_count": 0,
                        "first_seen": format_db_time(row["first_seen"]),
                        "last_seen": format_db_time(row["last_seen"]),
                        "first_statistic": None,
                        "last_statistic": None,
                    }

        for statistic in list_statistics(path, limit=None):
            statistic_id = statistic["statistic_id"]
            if not ENTITY_ID_RE.match(str(statistic_id)):
                continue
            entity = entities_by_id.setdefault(
                statistic_id,
                {
                    "entity_id": statistic_id,
                    "states_count": 0,
                    "statistics_count": 0,
                    "statistics_short_term_count": 0,
                    "first_seen": None,
                    "last_seen": None,
                    "first_statistic": None,
                    "last_statistic": None,
                },
            )
            entity["statistics_count"] = statistic["statistics_count"]
            entity["statistics_short_term_count"] = statistic["statistics_short_term_count"]
            entity["first_statistic"] = statistic["first_statistic"]
            entity["last_statistic"] = statistic["last_statistic"]

        entities = sorted(entities_by_id.values(), key=lambda item: item["entity_id"])
        return entities[:limit] if limit is not None else entities


def read_error(area: str, err: Exception) -> str:
    return f"{area}: {err}"


def query_rows_best_effort(
    conn: sqlite3.Connection,
    sql: str,
    params: list[Any] | tuple[Any, ...],
    errors: list[str],
    area: str,
):
    try:
        cursor = conn.execute(sql, params)
    except sqlite3.DatabaseError as err:
        errors.append(read_error(area, err))
        return
    while True:
        try:
            row = cursor.fetchone()
        except sqlite3.DatabaseError as err:
            errors.append(read_error(area, err))
            return
        if row is None:
            return
        yield row


def list_statistics_best_effort(path: Path, limit: int | None = 5000) -> list[dict[str, Any]]:
    if not path.exists() or not is_sqlite_file(path):
        return []

    summaries: dict[str, dict[str, Any]] = {}
    with open_db(path, readonly=True) as conn:
        try:
            tables = table_names(conn)
        except sqlite3.DatabaseError:
            return []
        if "statistics_meta" not in tables:
            return []

        try:
            meta_columns = table_columns(conn, "statistics_meta")
        except sqlite3.DatabaseError:
            return []
        meta_names = [str(column["name"]) for column in meta_columns]
        meta_pk = statistics_meta_primary_key(meta_columns)
        if not meta_pk or "statistic_id" not in meta_names:
            return []

        meta_sql = "SELECT statistic_id FROM statistics_meta WHERE statistic_id IS NOT NULL ORDER BY statistic_id"
        meta_params: list[Any] = []
        if limit is not None:
            meta_sql = f"{meta_sql} LIMIT ?"
            meta_params.append(limit)
        meta_errors: list[str] = []
        for row in query_rows_best_effort(conn, meta_sql, meta_params, meta_errors, "statistics_meta"):
            statistic_id = row["statistic_id"]
            summaries.setdefault(
                statistic_id,
                {
                    "statistic_id": statistic_id,
                    "statistics_count": 0,
                    "statistics_short_term_count": 0,
                    "first_statistic": None,
                    "last_statistic": None,
                    "read_errors": list(meta_errors),
                },
            )

        for table in STATISTICS_TABLES:
            if table not in tables:
                continue
            try:
                columns = column_names(conn, table)
            except sqlite3.DatabaseError:
                continue
            if "metadata_id" not in columns:
                continue
            start_column = start_time_column(columns)
            first_expr = f"MIN(t.{quote_identifier(start_column)})" if start_column else "NULL"
            last_expr = f"MAX(t.{quote_identifier(start_column)})" if start_column else "NULL"
            sql = f"""
                SELECT
                    sm.statistic_id AS statistic_id,
                    COUNT(*) AS row_count,
                    {first_expr} AS first_seen,
                    {last_expr} AS last_seen
                FROM {quote_identifier(table)} t
                JOIN statistics_meta sm ON sm.{quote_identifier(meta_pk)} = t.metadata_id
                GROUP BY sm.statistic_id
                ORDER BY sm.statistic_id
            """
            errors: list[str] = []
            for row in query_rows_best_effort(conn, sql, [], errors, table):
                statistic_id = row["statistic_id"]
                summary = summaries.setdefault(
                    statistic_id,
                    {
                        "statistic_id": statistic_id,
                        "statistics_count": 0,
                        "statistics_short_term_count": 0,
                        "first_statistic": None,
                        "last_statistic": None,
                        "read_errors": [],
                    },
                )
                count_key = "statistics_short_term_count" if table == "statistics_short_term" else "statistics_count"
                summary[count_key] = int(row["row_count"])
                first_seen = format_db_time(row["first_seen"])
                last_seen = format_db_time(row["last_seen"])
                if first_seen and (summary["first_statistic"] is None or str(first_seen) < str(summary["first_statistic"])):
                    summary["first_statistic"] = first_seen
                if last_seen and (summary["last_statistic"] is None or str(last_seen) > str(summary["last_statistic"])):
                    summary["last_statistic"] = last_seen
                if errors:
                    summary.setdefault("read_errors", []).extend(errors)

    return sorted(summaries.values(), key=lambda item: item["statistic_id"])[: limit or None]


def list_entities_best_effort(path: Path, limit: int | None = 5000) -> list[dict[str, Any]]:
    if not path.exists() or not is_sqlite_file(path):
        return []

    entities_by_id: dict[str, dict[str, Any]] = {}
    with open_db(path, readonly=True) as conn:
        try:
            tables = table_names(conn)
        except sqlite3.DatabaseError:
            return []

        if "states_meta" in tables:
            errors: list[str] = []
            for row in query_rows_best_effort(
                conn,
                "SELECT entity_id FROM states_meta WHERE entity_id IS NOT NULL ORDER BY entity_id",
                [],
                errors,
                "states_meta",
            ):
                entity_id = row["entity_id"]
                entities_by_id.setdefault(
                    entity_id,
                    {
                        "entity_id": entity_id,
                        "states_count": 0,
                        "statistics_count": 0,
                        "statistics_short_term_count": 0,
                        "first_seen": None,
                        "last_seen": None,
                        "first_statistic": None,
                        "last_statistic": None,
                        "read_errors": list(errors),
                    },
                )

        if "states" in tables:
            try:
                state_columns = column_names(conn, "states")
            except sqlite3.DatabaseError:
                state_columns = []
            time_expr = state_time_column(state_columns, alias="s") if state_columns else None
            first_expr = f"MIN({time_expr})" if time_expr else "NULL"
            last_expr = f"MAX({time_expr})" if time_expr else "NULL"

            if "states_meta" in tables and "metadata_id" in state_columns:
                sql = f"""
                    SELECT
                        sm.entity_id AS entity_id,
                        COUNT(s.metadata_id) AS states_count,
                        {first_expr} AS first_seen,
                        {last_expr} AS last_seen
                    FROM states_meta sm
                    LEFT JOIN states s ON sm.metadata_id = s.metadata_id
                    WHERE sm.entity_id IS NOT NULL
                    GROUP BY sm.entity_id
                    ORDER BY sm.entity_id
                """
            elif "entity_id" in state_columns:
                sql = f"""
                    SELECT
                        s.entity_id AS entity_id,
                        COUNT(*) AS states_count,
                        {first_expr} AS first_seen,
                        {last_expr} AS last_seen
                    FROM states s
                    WHERE s.entity_id IS NOT NULL
                    GROUP BY s.entity_id
                    ORDER BY s.entity_id
                """
            else:
                sql = ""

            if sql:
                errors = []
                for row in query_rows_best_effort(conn, sql, [], errors, "states"):
                    entity_id = row["entity_id"]
                    entity = entities_by_id.setdefault(
                        entity_id,
                        {
                            "entity_id": entity_id,
                            "states_count": 0,
                            "statistics_count": 0,
                            "statistics_short_term_count": 0,
                            "first_seen": None,
                            "last_seen": None,
                            "first_statistic": None,
                            "last_statistic": None,
                            "read_errors": [],
                        },
                    )
                    entity["states_count"] = int(row["states_count"] or 0)
                    entity["first_seen"] = format_db_time(row["first_seen"])
                    entity["last_seen"] = format_db_time(row["last_seen"])
                    if errors:
                        entity.setdefault("read_errors", []).extend(errors)

        for statistic in list_statistics_best_effort(path, limit=None):
            statistic_id = statistic["statistic_id"]
            if not ENTITY_ID_RE.match(str(statistic_id)):
                continue
            entity = entities_by_id.setdefault(
                statistic_id,
                {
                    "entity_id": statistic_id,
                    "states_count": 0,
                    "statistics_count": 0,
                    "statistics_short_term_count": 0,
                    "first_seen": None,
                    "last_seen": None,
                    "first_statistic": None,
                    "last_statistic": None,
                    "read_errors": [],
                },
            )
            entity["statistics_count"] = statistic["statistics_count"]
            entity["statistics_short_term_count"] = statistic["statistics_short_term_count"]
            entity["first_statistic"] = statistic["first_statistic"]
            entity["last_statistic"] = statistic["last_statistic"]
            if statistic.get("read_errors"):
                entity.setdefault("read_errors", []).extend(statistic["read_errors"])

    entities = sorted(entities_by_id.values(), key=lambda item: item["entity_id"])
    return entities[:limit] if limit is not None else entities


def analyze_database_best_effort(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "sqlite_header": False,
        "readable": False,
        "partial": False,
        "ok": False,
        "integrity": [],
        "foreign_key_errors": [],
        "tables": [],
        "states_count": 0,
        "entities_count": 0,
        "statistics_count": 0,
        "statistics_short_term_count": 0,
        "statistics_entities_count": 0,
        "first_statistic": None,
        "last_statistic": None,
        "first_state": None,
        "last_state": None,
        "quick_check": [],
        "journal_mode": None,
        "page_count": None,
        "freelist_count": None,
        "schema_version": None,
        "user_version": None,
        "sidecars": database_sidecar_files(path),
        "diagnostics": {"problems": [], "recommendations": [], "actions": []},
        "read_errors": [],
        "error": None,
    }

    def add_error(area: str, err: Exception | str) -> None:
        message = read_error(area, err if isinstance(err, Exception) else Exception(str(err)))
        result["read_errors"].append(message)
        result["partial"] = True

    if not path.exists():
        result["error"] = "Database file does not exist."
        result["diagnostics"] = build_database_diagnostics(result)
        return result
    if not is_sqlite_file(path):
        result["error"] = "File is not a SQLite database."
        result["diagnostics"] = build_database_diagnostics(result)
        return result

    result["sqlite_header"] = True
    try:
        with open_db(path, readonly=True) as conn:
            result["readable"] = True
            try:
                integrity_rows = conn.execute("PRAGMA integrity_check(20)").fetchall()
                result["integrity"] = [str(row[0]) for row in integrity_rows]
                result["ok"] = result["integrity"] == ["ok"]
                if not result["ok"]:
                    result["partial"] = True
                    result["read_errors"].extend(result["integrity"])
            except sqlite3.DatabaseError as err:
                add_error("integrity_check", err)

            try:
                quick_rows = conn.execute("PRAGMA quick_check(20)").fetchall()
                result["quick_check"] = [str(row[0]) for row in quick_rows]
            except sqlite3.DatabaseError as err:
                add_error("quick_check", err)

            for pragma in ("journal_mode", "page_count", "freelist_count", "schema_version", "user_version"):
                try:
                    result[pragma] = read_pragma(conn, pragma)
                except sqlite3.DatabaseError as err:
                    add_error(pragma, err)

            try:
                fk_rows = conn.execute("PRAGMA foreign_key_check").fetchmany(20)
                result["foreign_key_errors"] = [list(row) for row in fk_rows]
            except sqlite3.DatabaseError as err:
                add_error("foreign_key_check", err)

            try:
                tables = sorted(table_names(conn))
                result["tables"] = tables
            except sqlite3.DatabaseError as err:
                add_error("table_list", err)
                tables = []

            if "states" in tables:
                try:
                    state_columns = column_names(conn, "states")
                    result["states_count"] = int(conn.execute("SELECT COUNT(*) FROM states").fetchone()[0])
                    time_expr = state_time_column(state_columns)
                    if time_expr:
                        row = conn.execute(f"SELECT MIN({time_expr}), MAX({time_expr}) FROM states").fetchone()
                        result["first_state"] = format_db_time(row[0])
                        result["last_state"] = format_db_time(row[1])
                except sqlite3.DatabaseError as err:
                    add_error("states", err)

            if "statistics_meta" in tables:
                try:
                    result["statistics_entities_count"] = len(list_statistics_best_effort(path, limit=None))
                    result["entities_count"] = len(list_entities_best_effort(path, limit=None))
                except sqlite3.DatabaseError as err:
                    add_error("statistics_meta", err)

            for table in STATISTICS_TABLES:
                if table not in tables:
                    continue
                try:
                    columns = column_names(conn, table)
                    count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0])
                    if table == "statistics_short_term":
                        result["statistics_short_term_count"] = count
                    else:
                        result["statistics_count"] = count
                    start_column = start_time_column(columns)
                    if start_column:
                        row = conn.execute(
                            f"SELECT MIN({quote_identifier(start_column)}), MAX({quote_identifier(start_column)}) FROM {quote_identifier(table)}"
                        ).fetchone()
                        if row[0] is not None:
                            first = format_db_time(row[0])
                            last = format_db_time(row[1])
                            if result["first_statistic"] is None or str(first) < str(result["first_statistic"]):
                                result["first_statistic"] = first
                            if result["last_statistic"] is None or str(last) > str(result["last_statistic"]):
                                result["last_statistic"] = last
                except sqlite3.DatabaseError as err:
                    add_error(table, err)

            if not result["entities_count"]:
                result["entities_count"] = len(list_entities_best_effort(path, limit=None))
    except sqlite3.DatabaseError as err:
        result["error"] = str(err)
        result["partial"] = bool(result["sqlite_header"])

    if result["readable"] and result["ok"] is False and not result["error"]:
        result["error"] = "Database has integrity warnings, but readable parts can be used."
    result["diagnostics"] = build_database_diagnostics(result)
    return result


list_statistics = list_statistics_best_effort
list_entities = list_entities_best_effort
analyze_database = analyze_database_best_effort


def list_current_entities() -> dict[str, Any]:
    options = read_options()
    entities: dict[str, dict[str, Any]] = {}
    api_error = None

    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        request = urllib.request.Request(
            "http://supervisor/core/api/states",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list):
                for item in payload:
                    entity_id = item.get("entity_id")
                    if not entity_id:
                        continue
                    attributes = item.get("attributes") or {}
                    entities[entity_id] = {
                        "entity_id": entity_id,
                        "name": attributes.get("friendly_name") or entity_id,
                        "state": item.get("state"),
                        "source": "api",
                    }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as err:
            api_error = str(err)

    db_path = Path(str(options["database_path"]))
    if not entities and db_path.exists() and is_sqlite_file(db_path):
        recorder_page = paginated_database_entities(db_path, offset=0, limit=5000)
        for entity in recorder_page.get("entities", []):
            entity_id = entity["entity_id"]
            entities[entity_id] = {
                "entity_id": entity_id,
                "name": entity_id,
                "state": None,
                "source": "recorder",
            }
            entities[entity_id]["states_count"] = entity["states_count"]
            entities[entity_id]["first_seen"] = entity["first_seen"]
            entities[entity_id]["last_seen"] = entity["last_seen"]

    return {
        "entities": sorted(entities.values(), key=lambda item: item["entity_id"]),
        "api_error": api_error,
        "database_path": str(db_path),
    }


def check_cancel(cancel_callback: Any = None) -> None:
    if cancel_callback:
        cancel_callback()


def copy_stream(source: Any, destination: Path, cancel_callback: Any = None) -> None:
    with destination.open("wb") as output:
        while True:
            check_cancel(cancel_callback)
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)


def copy_path(source: Path, destination: Path, cancel_callback: Any = None) -> None:
    with source.open("rb") as input_file, destination.open("wb") as output:
        while True:
            check_cancel(cancel_callback)
            chunk = input_file.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
    try:
        shutil.copystat(source, destination)
    except OSError:
        pass


def link_or_copy_path(source: Path, destination: Path, cancel_callback: Any = None, prefer_link: bool = False) -> str:
    if prefer_link:
        try:
            os.link(source, destination)
            return "hardlink"
        except OSError:
            try:
                destination.symlink_to(source)
                return "symlink"
            except OSError:
                pass
    try:
        copy_path(source, destination, cancel_callback)
        return "copy"
    except OSError as err:
        raise AppError(f"Datei konnte nicht in den Cache uebernommen werden: {err}") from err


def supported_backup_file(path: Path) -> bool:
    normalized = path.name.lower()
    return normalized.endswith(BACKUP_FILE_EXTENSIONS)


def source_database_sidecar_paths() -> tuple[Path, Path, Path]:
    return (Path(f"{SOURCE_DB}-wal"), Path(f"{SOURCE_DB}-shm"), Path(f"{SOURCE_DB}-journal"))


def cleanup_source_database_files(include_meta: bool = False) -> None:
    paths = [SOURCE_DB, *source_database_sidecar_paths()]
    if include_meta:
        paths.extend([SOURCE_ORIGINAL, SOURCE_META])
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def paginate_items(items: list[dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
    total = len(items)
    bounded_limit = max(10, min(limit, 500))
    bounded_offset = max(0, min(offset, total if total else 0))
    page = items[bounded_offset : bounded_offset + bounded_limit]
    return {
        "items": page,
        "offset": bounded_offset,
        "limit": bounded_limit,
        "total": total,
        "has_next": bounded_offset + bounded_limit < total,
        "has_previous": bounded_offset > 0,
    }


def list_device_backups(offset: int = 0, limit: int = 100, filter_text: str = "") -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    if not BACKUP_DIR.exists():
        return {
            "backup_dir": str(BACKUP_DIR),
            "exists": False,
            "files": files,
            "offset": 0,
            "limit": limit,
            "total": 0,
            "has_next": False,
            "has_previous": False,
            "filter": filter_text,
        }

    backup_root = BACKUP_DIR.resolve()
    normalized_filter = filter_text.strip().lower()
    for path in BACKUP_DIR.rglob("*"):
        if not path.is_file() or not supported_backup_file(path):
            continue
        try:
            resolved = path.resolve()
            relative_path = resolved.relative_to(backup_root).as_posix()
            stat = path.stat()
        except (OSError, ValueError):
            continue
        if normalized_filter and normalized_filter not in relative_path.lower() and normalized_filter not in path.name.lower():
            continue
        files.append(
            {
                "id": relative_path,
                "name": path.name,
                "relative_path": relative_path,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )

    files.sort(key=lambda item: (item["modified"], item["relative_path"]), reverse=True)
    page = paginate_items(files, offset, limit)
    return {
        "backup_dir": str(BACKUP_DIR),
        "exists": True,
        "files": page["items"],
        "offset": page["offset"],
        "limit": page["limit"],
        "total": page["total"],
        "has_next": page["has_next"],
        "has_previous": page["has_previous"],
        "filter": filter_text,
    }


def resolve_device_backup(file_id: str) -> Path:
    if not file_id:
        raise AppError("No backup file was selected.")
    backup_root = BACKUP_DIR.resolve()
    path = (BACKUP_DIR / file_id).resolve()
    try:
        path.relative_to(backup_root)
    except ValueError as err:
        raise AppError("Backup path is outside the mounted backup directory.") from err
    if not path.is_file():
        raise AppError("Selected backup file does not exist.")
    if not supported_backup_file(path):
        raise AppError("Selected file is not a supported backup or SQLite file.")
    return path


def recovery_root() -> Path:
    options = read_options()
    current_db = Path(str(options["database_path"]))
    return current_db.parent


def corrupt_marker_parts(path: Path) -> tuple[str, str] | None:
    name = path.name
    marker = ".corrupt"
    marker_index = name.lower().find(marker)
    if marker_index < 0:
        return None
    return name[:marker_index], name[marker_index + len(marker):]


def is_corrupt_database_file(path: Path) -> bool:
    parts = corrupt_marker_parts(path)
    if not parts:
        return False
    base_name, _ = parts
    normalized_base = base_name.lower()
    if normalized_base.endswith(("-wal", "-shm", "-journal")):
        return False
    return normalized_base.endswith((".db", ".sqlite", ".sqlite3"))


def matching_corrupt_sidecars(path: Path) -> dict[str, Path]:
    parts = corrupt_marker_parts(path)
    if not parts:
        return {}
    base_name, suffix = parts
    sidecars: dict[str, Path] = {}
    for kind in ("wal", "shm", "journal"):
        candidates = [
            path.parent / f"{base_name}-{kind}.corrupt{suffix}",
            path.parent / f"{base_name}-{kind}",
        ]
        for candidate in candidates:
            if candidate.is_file():
                sidecars[kind] = candidate
                break
    return sidecars


def list_corrupt_databases(offset: int = 0, limit: int = 100, filter_text: str = "") -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    root = recovery_root()
    if not root.exists():
        return {
            "config_dir": str(root),
            "exists": False,
            "files": files,
            "offset": 0,
            "limit": limit,
            "total": 0,
            "has_next": False,
            "has_previous": False,
            "filter": filter_text,
        }

    root_resolved = root.resolve()
    normalized_filter = filter_text.strip().lower()
    for path in root.glob("*corrupt*"):
        if not path.is_file() or not is_corrupt_database_file(path):
            continue
        try:
            resolved = path.resolve()
            relative_path = resolved.relative_to(root_resolved).as_posix()
            stat = path.stat()
        except (OSError, ValueError):
            continue
        if normalized_filter and normalized_filter not in relative_path.lower() and normalized_filter not in path.name.lower():
            continue
        sidecars = {
            kind: file_info(sidecar)
            for kind, sidecar in matching_corrupt_sidecars(path).items()
        }
        files.append(
            {
                "id": relative_path,
                "name": path.name,
                "relative_path": relative_path,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "sqlite_header": is_sqlite_file(path),
                "sidecars": sidecars,
                "sidecar_count": len(sidecars),
                "sidecar_size_bytes": sum(int(info.get("size_bytes") or 0) for info in sidecars.values()),
            }
        )

    files.sort(key=lambda item: (item["modified"], item["relative_path"]), reverse=True)
    page = paginate_items(files, offset, limit)
    return {
        "config_dir": str(root),
        "exists": True,
        "files": page["items"],
        "offset": page["offset"],
        "limit": page["limit"],
        "total": page["total"],
        "has_next": page["has_next"],
        "has_previous": page["has_previous"],
        "filter": filter_text,
    }


def resolve_corrupt_database(file_id: str) -> Path:
    if not file_id:
        raise AppError("No corrupt database was selected.")
    root = recovery_root().resolve()
    path = (root / file_id).resolve()
    try:
        path.relative_to(root)
    except ValueError as err:
        raise AppError("Corrupt database path is outside the configured database directory.") from err
    if not path.is_file():
        raise AppError("Selected corrupt database does not exist.")
    if not is_corrupt_database_file(path):
        raise AppError("Selected file is not a supported corrupt Recorder database.")
    if not is_sqlite_file(path):
        raise AppError("Selected corrupt database has no SQLite header.")
    return path


def paginate_entities(entities: list[dict[str, Any]], offset: int, limit: int, filter_text: str = "") -> dict[str, Any]:
    normalized_filter = filter_text.strip().lower()
    if normalized_filter:
        entities = [
            entity
            for entity in entities
            if normalized_filter in str(entity.get("entity_id", "")).lower()
        ]

    total = len(entities)
    bounded_limit = max(10, min(limit, 500))
    bounded_offset = max(0, min(offset, total if total else 0))
    page = entities[bounded_offset : bounded_offset + bounded_limit]
    return {
        "entities": page,
        "offset": bounded_offset,
        "limit": bounded_limit,
        "total": total,
        "has_next": bounded_offset + bounded_limit < total,
        "has_previous": bounded_offset > 0,
        "filter": filter_text,
    }


def empty_entity_summary(entity_id: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "datapoints_count": 0,
        "states_count": 0,
        "statistics_count": 0,
        "statistics_short_term_count": 0,
        "first_seen": None,
        "last_seen": None,
        "first_statistic": None,
        "last_statistic": None,
    }


def entity_summary_first_time(entity: dict[str, Any]) -> str | None:
    values = [
        str(value)
        for value in (entity.get("first_seen"), entity.get("first_statistic"))
        if value
    ]
    return min(values) if values else None


def entity_summary_last_time(entity: dict[str, Any]) -> str | None:
    values = [
        str(value)
        for value in (entity.get("last_seen"), entity.get("last_statistic"))
        if value
    ]
    return max(values) if values else None


def sort_entity_summaries(
    entities: list[dict[str, Any]],
    sort_by: str,
    sort_order: str,
) -> list[dict[str, Any]]:
    reverse = sort_order == "desc"

    def metric(entity: dict[str, Any]) -> int | str | None:
        if sort_by == "datapoints":
            return int(entity.get("datapoints_count") or 0)
        if sort_by == "states":
            return int(entity.get("states_count") or 0)
        if sort_by == "statistics":
            return int(entity.get("statistics_count") or 0) + int(entity.get("statistics_short_term_count") or 0)
        if sort_by == "first_seen":
            return entity_summary_first_time(entity)
        if sort_by == "last_seen":
            return entity_summary_last_time(entity)
        return str(entity.get("entity_id") or "")

    with_values = [entity for entity in entities if metric(entity) not in (None, "")]
    without_values = [entity for entity in entities if metric(entity) in (None, "")]
    return [
        *sorted(
            with_values,
            key=lambda entity: (metric(entity), str(entity.get("entity_id") or "")),
            reverse=reverse,
        ),
        *sorted(without_values, key=lambda entity: str(entity.get("entity_id") or "")),
    ]


def paginated_database_entities(
    path: Path,
    offset: int = 0,
    limit: int = 100,
    filter_text: str = "",
    immutable: bool = False,
    sort_by: str = "entity_id",
    sort_order: str = "asc",
) -> dict[str, Any]:
    bounded_limit = max(10, min(limit, 500))
    bounded_offset = max(0, offset)
    normalized_filter = filter_text.strip().lower()
    normalized_sort = sort_by if sort_by in {"entity_id", "datapoints", "states", "statistics", "first_seen", "last_seen"} else "entity_id"
    normalized_order = "desc" if str(sort_order).lower() == "desc" else "asc"
    if not path.exists() or not is_sqlite_file(path):
        return {
            "entities": [],
            "offset": 0,
            "limit": bounded_limit,
            "total": 0,
            "has_next": False,
            "has_previous": False,
            "filter": filter_text,
            "sort": normalized_sort,
            "order": normalized_order,
        }

    try:
        with open_db(path, readonly=True, immutable=immutable) as conn:
            tables = table_names(conn)
            selects: list[str] = []
            params: list[Any] = []

            if "states_meta" in tables:
                state_meta_columns = column_names(conn, "states_meta")
                if "entity_id" in state_meta_columns:
                    sql = "SELECT entity_id FROM states_meta WHERE entity_id IS NOT NULL"
                    if normalized_filter:
                        sql = f"{sql} AND lower(entity_id) LIKE ?"
                        params.append(f"%{normalized_filter}%")
                    selects.append(sql)

            if "statistics_meta" in tables:
                statistics_meta_columns = column_names(conn, "statistics_meta")
                if "statistic_id" in statistics_meta_columns:
                    sql = "SELECT statistic_id AS entity_id FROM statistics_meta WHERE statistic_id IS NOT NULL AND instr(statistic_id, '.') > 0"
                    if normalized_filter:
                        sql = f"{sql} AND lower(statistic_id) LIKE ?"
                        params.append(f"%{normalized_filter}%")
                    selects.append(sql)

            if not selects and "states" in tables:
                state_columns = column_names(conn, "states")
                if "entity_id" in state_columns:
                    sql = "SELECT DISTINCT entity_id FROM states WHERE entity_id IS NOT NULL"
                    if normalized_filter:
                        sql = f"{sql} AND lower(entity_id) LIKE ?"
                        params.append(f"%{normalized_filter}%")
                    selects.append(sql)

            if not selects:
                return {
                    "entities": [],
                    "offset": 0,
                    "limit": bounded_limit,
                    "total": 0,
                    "has_next": False,
                    "has_previous": False,
                    "filter": filter_text,
                    "sort": normalized_sort,
                    "order": normalized_order,
                }

            union_sql = " UNION ".join(selects)
            total = int(conn.execute(f"SELECT COUNT(*) FROM ({union_sql}) entity_ids", params).fetchone()[0])
            bounded_offset = min(bounded_offset, total if total else 0)
            if normalized_sort == "entity_id":
                sql_order = "DESC" if normalized_order == "desc" else "ASC"
                entity_rows = conn.execute(
                    f"SELECT entity_id FROM ({union_sql}) entity_ids ORDER BY entity_id {sql_order} LIMIT ? OFFSET ?",
                    [*params, bounded_limit, bounded_offset],
                ).fetchall()
            else:
                entity_rows = conn.execute(
                    f"SELECT entity_id FROM ({union_sql}) entity_ids ORDER BY entity_id",
                    params,
                ).fetchall()
            entity_ids = [str(row["entity_id"]) for row in entity_rows if row["entity_id"]]
            entities = {entity_id: empty_entity_summary(entity_id) for entity_id in entity_ids}

            if entity_ids and "states" in tables:
                state_columns = column_names(conn, "states")
                state_meta_names_for_counts = column_names(conn, "states_meta") if "states_meta" in tables else []
                time_column = state_time_column_name(state_columns)
                first_expr = f"MIN({quote_identifier(time_column)})" if time_column else "NULL"
                last_expr = f"MAX({quote_identifier(time_column)})" if time_column else "NULL"

                if "states_meta" in tables and "metadata_id" in state_columns and "metadata_id" in state_meta_names_for_counts:
                    placeholders = ", ".join("?" for _ in entity_ids)
                    metadata_rows = conn.execute(
                        f"SELECT metadata_id, entity_id FROM states_meta WHERE entity_id IN ({placeholders})",
                        entity_ids,
                    ).fetchall()
                    for metadata in metadata_rows:
                        entity_id = str(metadata["entity_id"])
                        if entity_id not in entities:
                            continue
                        row = conn.execute(
                            f"SELECT COUNT(*) AS row_count, {first_expr} AS first_seen, {last_expr} AS last_seen FROM states WHERE metadata_id = ?",
                            (metadata["metadata_id"],),
                        ).fetchone()
                        entities[entity_id]["states_count"] = int(row["row_count"] or 0)
                        entities[entity_id]["first_seen"] = format_db_time(row["first_seen"])
                        entities[entity_id]["last_seen"] = format_db_time(row["last_seen"])
                elif "entity_id" in state_columns:
                    for entity_id in entity_ids:
                        row = conn.execute(
                            f"SELECT COUNT(*) AS row_count, {first_expr} AS first_seen, {last_expr} AS last_seen FROM states WHERE entity_id = ?",
                            (entity_id,),
                        ).fetchone()
                        entities[entity_id]["states_count"] = int(row["row_count"] or 0)
                        entities[entity_id]["first_seen"] = format_db_time(row["first_seen"])
                        entities[entity_id]["last_seen"] = format_db_time(row["last_seen"])

            if entity_ids and "statistics_meta" in tables:
                statistics_meta_columns = table_columns(conn, "statistics_meta")
                statistics_meta_names = [str(column["name"]) for column in statistics_meta_columns]
                statistics_meta_pk = statistics_meta_primary_key(statistics_meta_columns)
                if statistics_meta_pk and "statistic_id" in statistics_meta_names:
                    placeholders = ", ".join("?" for _ in entity_ids)
                    metadata_rows = conn.execute(
                        f"""
                        SELECT {quote_identifier(statistics_meta_pk)} AS metadata_id, statistic_id
                        FROM statistics_meta
                        WHERE statistic_id IN ({placeholders})
                        """,
                        entity_ids,
                    ).fetchall()
                    for metadata in metadata_rows:
                        entity_id = str(metadata["statistic_id"])
                        if entity_id not in entities:
                            continue
                        for table in STATISTICS_TABLES:
                            if table not in tables:
                                continue
                            columns = column_names(conn, table)
                            if "metadata_id" not in columns:
                                continue
                            start_column = start_time_column(columns)
                            first_expr = f"MIN({quote_identifier(start_column)})" if start_column else "NULL"
                            last_expr = f"MAX({quote_identifier(start_column)})" if start_column else "NULL"
                            row = conn.execute(
                                f"""
                                SELECT COUNT(*) AS row_count, {first_expr} AS first_seen, {last_expr} AS last_seen
                                FROM {quote_identifier(table)}
                                WHERE metadata_id = ?
                                """,
                                (metadata["metadata_id"],),
                            ).fetchone()
                            count_key = "statistics_short_term_count" if table == "statistics_short_term" else "statistics_count"
                            entities[entity_id][count_key] = int(row["row_count"] or 0)
                            first_seen = format_db_time(row["first_seen"])
                            last_seen = format_db_time(row["last_seen"])
                            if first_seen and (entities[entity_id]["first_statistic"] is None or str(first_seen) < str(entities[entity_id]["first_statistic"])):
                                entities[entity_id]["first_statistic"] = first_seen
                            if last_seen and (entities[entity_id]["last_statistic"] is None or str(last_seen) > str(entities[entity_id]["last_statistic"])):
                                entities[entity_id]["last_statistic"] = last_seen

            page_entities = [entities[entity_id] for entity_id in entity_ids]
            for entity in page_entities:
                entity["datapoints_count"] = (
                    int(entity.get("states_count") or 0)
                    + int(entity.get("statistics_count") or 0)
                    + int(entity.get("statistics_short_term_count") or 0)
                )

            if normalized_sort != "entity_id":
                page_entities = sort_entity_summaries(page_entities, normalized_sort, normalized_order)
                page_entities = page_entities[bounded_offset : bounded_offset + bounded_limit]

            return {
                "entities": page_entities,
                "offset": bounded_offset,
                "limit": bounded_limit,
                "total": total,
                "has_next": bounded_offset + bounded_limit < total,
                "has_previous": bounded_offset > 0,
                "filter": filter_text,
                "sort": normalized_sort,
                "order": normalized_order,
            }
    except sqlite3.DatabaseError as err:
        if not immutable:
            page = paginated_database_entities(path, offset, limit, filter_text, immutable=True, sort_by=sort_by, sort_order=sort_order)
            if page.get("error"):
                page["error"] = f"{err}; immutable fallback: {page['error']}"
            else:
                page["read_warning"] = f"{err}; immutable fallback active"
            return page
        return {
            "entities": [],
            "offset": 0,
            "limit": bounded_limit,
            "total": 0,
            "has_next": False,
            "has_previous": False,
            "filter": filter_text,
            "sort": normalized_sort,
            "order": normalized_order,
            "error": str(err),
        }


def database_entity_exists(path: Path, entity_id: str, immutable: bool = False) -> bool:
    if not entity_id or not path.exists() or not is_sqlite_file(path):
        return False
    try:
        with open_db(path, readonly=True, immutable=immutable) as conn:
            tables = table_names(conn)
            if "states_meta" in tables and "entity_id" in column_names(conn, "states_meta"):
                row = conn.execute("SELECT 1 FROM states_meta WHERE entity_id = ? LIMIT 1", (entity_id,)).fetchone()
                if row is not None:
                    return True
            if "statistics_meta" in tables and "statistic_id" in column_names(conn, "statistics_meta"):
                row = conn.execute("SELECT 1 FROM statistics_meta WHERE statistic_id = ? LIMIT 1", (entity_id,)).fetchone()
                if row is not None:
                    return True
            if "states" in tables:
                state_columns = column_names(conn, "states")
                if "entity_id" in state_columns:
                    row = conn.execute("SELECT 1 FROM states WHERE entity_id = ? LIMIT 1", (entity_id,)).fetchone()
                    return row is not None
    except sqlite3.DatabaseError:
        if not immutable:
            return database_entity_exists(path, entity_id, immutable=True)
        return False
    return False


def current_database_path() -> Path:
    options = read_options()
    return Path(str(options["database_path"]))


def paginated_current_entities(query: dict[str, list[str]]) -> dict[str, Any]:
    current_db = current_database_path()
    page = paginated_database_entities(
        current_db,
        query_int(query, "offset", 0),
        query_int(query, "limit", 100),
        query.get("filter", [""])[0],
        sort_by=query.get("sort", ["entity_id"])[0],
        sort_order=query.get("order", ["asc"])[0],
    )
    page["database_path"] = str(current_db)
    return page


def normalize_purge_entity_ids(payload: dict[str, Any]) -> list[str]:
    raw_entity_ids = payload.get("entity_ids")
    if raw_entity_ids is None:
        raw_entity_ids = [payload.get("entity_id")]
    if not isinstance(raw_entity_ids, list):
        raise AppError("Entity selection needs to be a list.")

    entity_ids: list[str] = []
    for raw_entity_id in raw_entity_ids:
        entity_id = str(raw_entity_id or "").strip()
        if not entity_id:
            continue
        if not ENTITY_ID_RE.match(entity_id):
            raise AppError(f"Entity id is invalid: {entity_id}")
        if entity_id not in entity_ids:
            entity_ids.append(entity_id)

    if not entity_ids:
        raise AppError("At least one entity needs to be selected.")
    if len(entity_ids) > 500:
        raise AppError("At most 500 entities can be purged at once.")
    return entity_ids


def purge_time_bounds(payload: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    start = parse_datetime_value(payload.get("start"))
    end = parse_datetime_value(payload.get("end"))
    if start and end and start > end:
        raise AppError("The purge start time must be before the end time.")
    return start, end


def qualified_column(column: str, alias: str | None = None) -> str:
    return f"{alias}.{quote_identifier(column)}" if alias else quote_identifier(column)


def placeholders(values: list[Any]) -> str:
    return ", ".join("?" for _ in values)


def append_time_where(
    parts: list[str],
    params: list[Any],
    column: str | None,
    start: datetime | None,
    end: datetime | None,
    alias: str | None = None,
) -> bool:
    if not start and not end:
        return True
    if not column:
        return False
    qualified = qualified_column(column, alias)
    if start:
        parts.append(f"{qualified} >= ?")
        params.append(datetime_bound_for_column(start, column))
    if end:
        parts.append(f"{qualified} <= ?")
        params.append(datetime_bound_for_column(end, column))
    return True


def existing_database_entity_ids(conn: sqlite3.Connection, tables: set[str], entity_ids: list[str]) -> set[str]:
    existing: set[str] = set()
    if not entity_ids:
        return existing
    sql_placeholders = placeholders(entity_ids)
    if "states_meta" in tables and "entity_id" in column_names(conn, "states_meta"):
        rows = conn.execute(
            f"SELECT entity_id FROM states_meta WHERE entity_id IN ({sql_placeholders})",
            entity_ids,
        ).fetchall()
        existing.update(str(row["entity_id"]) for row in rows)
    if "statistics_meta" in tables and "statistic_id" in column_names(conn, "statistics_meta"):
        rows = conn.execute(
            f"SELECT statistic_id FROM statistics_meta WHERE statistic_id IN ({sql_placeholders})",
            entity_ids,
        ).fetchall()
        existing.update(str(row["statistic_id"]) for row in rows)
    if "states" in tables:
        state_columns = column_names(conn, "states")
        if "entity_id" in state_columns:
            rows = conn.execute(
                f"SELECT DISTINCT entity_id FROM states WHERE entity_id IN ({sql_placeholders})",
                entity_ids,
            ).fetchall()
            existing.update(str(row["entity_id"]) for row in rows)
    return existing


def state_where_for_entities(
    conn: sqlite3.Connection,
    tables: set[str],
    entity_ids: list[str],
    start: datetime | None,
    end: datetime | None,
    alias: str | None = None,
) -> tuple[str, list[Any], list[str]]:
    if "states" not in tables or not entity_ids:
        return "", [], []
    state_columns = column_names(conn, "states")
    parts: list[str] = []
    params: list[Any] = []
    warnings: list[str] = []

    if "states_meta" in tables and "metadata_id" in state_columns:
        state_meta_columns = column_names(conn, "states_meta")
        if "metadata_id" in state_meta_columns and "entity_id" in state_meta_columns:
            rows = conn.execute(
                f"SELECT metadata_id FROM states_meta WHERE entity_id IN ({placeholders(entity_ids)})",
                entity_ids,
            ).fetchall()
            metadata_ids = [row["metadata_id"] for row in rows]
            if metadata_ids:
                parts.append(f"{qualified_column('metadata_id', alias)} IN ({placeholders(metadata_ids)})")
                params.extend(metadata_ids)
    elif "entity_id" in state_columns:
        parts.append(f"{qualified_column('entity_id', alias)} IN ({placeholders(entity_ids)})")
        params.extend(entity_ids)

    if not parts:
        return "", [], warnings

    time_column = state_time_column_name(state_columns)
    if not append_time_where(parts, params, time_column, start, end, alias):
        warnings.append("States wurden uebersprungen, weil keine Zeitspalte vorhanden ist.")
        return "", [], warnings
    return " AND ".join(parts), params, warnings


def statistics_metadata_ids_for_entities(
    conn: sqlite3.Connection,
    tables: set[str],
    entity_ids: list[str],
) -> list[Any]:
    if "statistics_meta" not in tables or not entity_ids:
        return []
    statistics_meta_columns = table_columns(conn, "statistics_meta")
    statistics_meta_names = [str(column["name"]) for column in statistics_meta_columns]
    statistics_meta_pk = statistics_meta_primary_key(statistics_meta_columns)
    if not statistics_meta_pk or "statistic_id" not in statistics_meta_names:
        return []
    rows = conn.execute(
        f"""
        SELECT {quote_identifier(statistics_meta_pk)} AS metadata_id
        FROM statistics_meta
        WHERE statistic_id IN ({placeholders(entity_ids)})
        """,
        entity_ids,
    ).fetchall()
    return [row["metadata_id"] for row in rows]


def statistics_where_for_entities(
    conn: sqlite3.Connection,
    tables: set[str],
    table: str,
    entity_ids: list[str],
    start: datetime | None,
    end: datetime | None,
) -> tuple[str, list[Any], list[str]]:
    if table not in tables or "metadata_id" not in column_names(conn, table):
        return "", [], []
    metadata_ids = statistics_metadata_ids_for_entities(conn, tables, entity_ids)
    if not metadata_ids:
        return "", [], []
    parts = [f"metadata_id IN ({placeholders(metadata_ids)})"]
    params: list[Any] = list(metadata_ids)
    start_column = start_time_column(column_names(conn, table))
    if not append_time_where(parts, params, start_column, start, end):
        return "", [], [f"{table} wurde uebersprungen, weil keine Zeitspalte vorhanden ist."]
    return " AND ".join(parts), params, []


def empty_purge_counts() -> dict[str, int]:
    return {
        "states": 0,
        "state_attributes": 0,
        "states_meta": 0,
        "statistics": 0,
        "statistics_short_term": 0,
        "statistics_meta": 0,
        "total_datapoints": 0,
    }


def normalize_purge_maintenance(payload: dict[str, Any]) -> str:
    mode = str(payload.get("maintenance") or "none").strip().lower()
    if mode not in {"none", "checkpoint", "vacuum", "checkpoint_vacuum"}:
        raise AppError("Unsupported purge maintenance mode.")
    return mode


def state_metadata_ids_for_entities(
    conn: sqlite3.Connection,
    tables: set[str],
    entity_ids: list[str],
) -> list[Any]:
    if "states_meta" not in tables or "states" not in tables or not entity_ids:
        return []
    state_columns = column_names(conn, "states")
    state_meta_columns = column_names(conn, "states_meta")
    if "metadata_id" not in state_columns or "metadata_id" not in state_meta_columns or "entity_id" not in state_meta_columns:
        return []
    rows = conn.execute(
        f"""
        SELECT metadata_id
        FROM states_meta
        WHERE entity_id IN ({placeholders(entity_ids)})
        """,
        entity_ids,
    ).fetchall()
    return [row["metadata_id"] for row in rows]


def count_selected_state_metadata_rows(
    conn: sqlite3.Connection,
    metadata_id: Any,
    start: datetime | None,
    end: datetime | None,
) -> int:
    state_columns = column_names(conn, "states")
    parts = ["metadata_id = ?"]
    params: list[Any] = [metadata_id]
    if not append_time_where(parts, params, state_time_column_name(state_columns), start, end):
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS row_count FROM states WHERE {' AND '.join(parts)}",
        params,
    ).fetchone()
    return int(row["row_count"] or 0)


def count_selected_statistics_metadata_rows(
    conn: sqlite3.Connection,
    tables: set[str],
    metadata_id: Any,
    start: datetime | None,
    end: datetime | None,
) -> int:
    total = 0
    for table in STATISTICS_TABLES:
        if table not in tables or "metadata_id" not in column_names(conn, table):
            continue
        columns = column_names(conn, table)
        parts = ["metadata_id = ?"]
        params: list[Any] = [metadata_id]
        if not append_time_where(parts, params, start_time_column(columns), start, end):
            continue
        row = conn.execute(
            f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table)} WHERE {' AND '.join(parts)}",
            params,
        ).fetchone()
        total += int(row["row_count"] or 0)
    return total


def count_all_statistics_metadata_rows(conn: sqlite3.Connection, tables: set[str], metadata_id: Any) -> int:
    total = 0
    for table in STATISTICS_TABLES:
        if table not in tables or "metadata_id" not in column_names(conn, table):
            continue
        row = conn.execute(
            f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table)} WHERE metadata_id = ?",
            (metadata_id,),
        ).fetchone()
        total += int(row["row_count"] or 0)
    return total


def preview_empty_metadata_cleanup(
    conn: sqlite3.Connection,
    tables: set[str],
    entity_ids: list[str],
    start: datetime | None,
    end: datetime | None,
) -> dict[str, int]:
    counts = {"states_meta": 0, "statistics_meta": 0}

    for metadata_id in state_metadata_ids_for_entities(conn, tables, entity_ids):
        row = conn.execute(
            "SELECT COUNT(*) AS row_count FROM states WHERE metadata_id = ?",
            (metadata_id,),
        ).fetchone()
        total_rows = int(row["row_count"] or 0)
        selected_rows = count_selected_state_metadata_rows(conn, metadata_id, start, end)
        if total_rows - selected_rows <= 0:
            counts["states_meta"] += 1

    for metadata_id in statistics_metadata_ids_for_entities(conn, tables, entity_ids):
        total_rows = count_all_statistics_metadata_rows(conn, tables, metadata_id)
        selected_rows = count_selected_statistics_metadata_rows(conn, tables, metadata_id, start, end)
        if total_rows - selected_rows <= 0:
            counts["statistics_meta"] += 1

    return counts


def cleanup_empty_entity_metadata(
    conn: sqlite3.Connection,
    tables: set[str],
    entity_ids: list[str],
) -> dict[str, int]:
    counts = {"states_meta": 0, "statistics_meta": 0}

    state_metadata_ids = state_metadata_ids_for_entities(conn, tables, entity_ids)
    if state_metadata_ids and "states_meta" in tables:
        for metadata_id in state_metadata_ids:
            row = conn.execute(
                "SELECT 1 FROM states WHERE metadata_id = ? LIMIT 1",
                (metadata_id,),
            ).fetchone()
            if row is not None:
                continue
            cursor = conn.execute("DELETE FROM states_meta WHERE metadata_id = ?", (metadata_id,))
            counts["states_meta"] += max(0, int(cursor.rowcount or 0))

    statistics_meta_columns = table_columns(conn, "statistics_meta") if "statistics_meta" in tables else []
    statistics_meta_pk = statistics_meta_primary_key(statistics_meta_columns)
    if statistics_meta_pk:
        for metadata_id in statistics_metadata_ids_for_entities(conn, tables, entity_ids):
            if count_all_statistics_metadata_rows(conn, tables, metadata_id) > 0:
                continue
            cursor = conn.execute(
                f"DELETE FROM statistics_meta WHERE {quote_identifier(statistics_meta_pk)} = ?",
                (metadata_id,),
            )
            counts["statistics_meta"] += max(0, int(cursor.rowcount or 0))

    return counts


def run_purge_database_maintenance(current_db: Path, mode: str) -> dict[str, Any]:
    result: dict[str, Any] = {"mode": mode}
    if mode == "none":
        return result

    if mode in {"checkpoint", "checkpoint_vacuum"}:
        with open_db(current_db, readonly=False, timeout=60) as conn:
            conn.execute("PRAGMA busy_timeout = 60000")
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            result["checkpoint"] = dict(row) if row is not None and row.keys() else (list(row) if row is not None else None)

    if mode in {"vacuum", "checkpoint_vacuum"}:
        before_size = current_db.stat().st_size if current_db.exists() else None
        started_at = now_iso()
        with open_db(current_db, readonly=False, timeout=120) as conn:
            conn.execute("PRAGMA busy_timeout = 120000")
            conn.execute("VACUUM")
        after_size = current_db.stat().st_size if current_db.exists() else None
        result["vacuum"] = {
            "started_at": started_at,
            "finished_at": now_iso(),
            "before_size_bytes": before_size,
            "after_size_bytes": after_size,
        }

    return result


def count_table_range(
    conn: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: list[Any],
    time_column: str | None,
) -> tuple[int, str | None, str | None]:
    if not where_sql:
        return 0, None, None
    if time_column:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS row_count,
                   MIN({quote_identifier(time_column)}) AS first_seen,
                   MAX({quote_identifier(time_column)}) AS last_seen
            FROM {quote_identifier(table)}
            WHERE {where_sql}
            """,
            params,
        ).fetchone()
        return int(row["row_count"] or 0), format_db_time(row["first_seen"]), format_db_time(row["last_seen"])
    row = conn.execute(
        f"SELECT COUNT(*) AS row_count FROM {quote_identifier(table)} WHERE {where_sql}",
        params,
    ).fetchone()
    return int(row["row_count"] or 0), None, None


def total_database_datapoints(conn: sqlite3.Connection, tables: set[str]) -> int:
    total = 0
    if "states" in tables:
        total += int(conn.execute("SELECT COUNT(*) FROM states").fetchone()[0])
    for table in STATISTICS_TABLES:
        if table in tables:
            total += int(conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}").fetchone()[0])
    return total


def preview_entity_history_purge(payload: dict[str, Any]) -> dict[str, Any]:
    entity_ids = normalize_purge_entity_ids(payload)
    start, end = purge_time_bounds(payload)
    cleanup_metadata = bool(payload.get("cleanup_metadata", False))
    maintenance = normalize_purge_maintenance(payload)
    current_db = current_database_path()
    if not current_db.exists():
        raise AppError("Current Home Assistant database was not found.")
    if not is_sqlite_file(current_db):
        raise AppError("Current Home Assistant database path does not point to a SQLite database.")

    with open_db(current_db, readonly=True) as conn:
        tables = table_names(conn)
        existing = existing_database_entity_ids(conn, tables, entity_ids)
        missing = [entity_id for entity_id in entity_ids if entity_id not in existing]
        if missing:
            raise AppError("One or more entities were not found in the current database.", HTTPStatus.NOT_FOUND, {"missing_entities": missing})

        totals = empty_purge_counts()
        warnings: list[str] = []
        entities: list[dict[str, Any]] = []
        state_columns = column_names(conn, "states") if "states" in tables else []
        state_time_column = state_time_column_name(state_columns)

        for entity_id in entity_ids:
            entity_counts = empty_purge_counts()
            first_seen = None
            last_seen = None
            first_statistic = None
            last_statistic = None

            state_where, state_params, state_warnings = state_where_for_entities(conn, tables, [entity_id], start, end)
            warnings.extend(state_warnings)
            if state_where:
                count, first_seen, last_seen = count_table_range(conn, "states", state_where, state_params, state_time_column)
                entity_counts["states"] = count

            for table in STATISTICS_TABLES:
                statistics_where, statistics_params, statistics_warnings = statistics_where_for_entities(conn, tables, table, [entity_id], start, end)
                warnings.extend(statistics_warnings)
                start_column = start_time_column(column_names(conn, table)) if table in tables else None
                count, table_first, table_last = count_table_range(conn, table, statistics_where, statistics_params, start_column)
                entity_counts[table] = count
                if table_first and (first_statistic is None or str(table_first) < str(first_statistic)):
                    first_statistic = table_first
                if table_last and (last_statistic is None or str(table_last) > str(last_statistic)):
                    last_statistic = table_last

            entity_counts["total_datapoints"] = (
                entity_counts["states"]
                + entity_counts["statistics"]
                + entity_counts["statistics_short_term"]
            )
            for key in totals:
                totals[key] += entity_counts[key]
            entities.append(
                {
                    "entity_id": entity_id,
                    "deleted": entity_counts,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "first_statistic": first_statistic,
                    "last_statistic": last_statistic,
                }
            )

        state_where, state_params, state_warnings = state_where_for_entities(conn, tables, entity_ids, start, end)
        warnings.extend(state_warnings)
        if state_where and "state_attributes" in tables and "attributes_id" in state_columns and "attributes_id" in column_names(conn, "state_attributes"):
            remaining_where, remaining_params, _ = state_where_for_entities(conn, tables, entity_ids, start, end, alias="remaining")
            if remaining_where:
                row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS row_count
                    FROM (
                        SELECT DISTINCT attributes_id
                        FROM states
                        WHERE {state_where} AND attributes_id IS NOT NULL
                    ) selected
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM states remaining
                        WHERE remaining.attributes_id = selected.attributes_id
                        AND NOT ({remaining_where})
                    )
                    """,
                    [*state_params, *remaining_params],
                ).fetchone()
                totals["state_attributes"] = int(row["row_count"] or 0)

        database_size = current_db.stat().st_size
        database_datapoints = total_database_datapoints(conn, tables)
        estimated_bytes = 0
        if database_datapoints > 0 and totals["total_datapoints"] > 0:
            estimated_bytes = max(1, int(database_size * (totals["total_datapoints"] / database_datapoints)))

        if cleanup_metadata:
            metadata_cleanup = preview_empty_metadata_cleanup(conn, tables, entity_ids, start, end)
            totals["states_meta"] = metadata_cleanup["states_meta"]
            totals["statistics_meta"] = metadata_cleanup["statistics_meta"]

    unique_warnings = list(dict.fromkeys(warnings))
    return {
        "entity_ids": entity_ids,
        "entity_count": len(entity_ids),
        "cleanup_metadata": cleanup_metadata,
        "maintenance": maintenance,
        "time_range": {
            "start": start.isoformat().replace("+00:00", "Z") if start else None,
            "end": end.isoformat().replace("+00:00", "Z") if end else None,
        },
        "entities": entities,
        "deleted": totals,
        "database_size_bytes": database_size,
        "estimated_selected_bytes": estimated_bytes,
        "estimated": True,
        "warnings": unique_warnings,
    }


def purge_entity_histories(payload: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    preview = preview_entity_history_purge(payload)
    entity_ids = list(preview["entity_ids"])
    start, end = purge_time_bounds(payload)
    cleanup_metadata = bool(preview.get("cleanup_metadata", False))
    maintenance = str(preview.get("maintenance") or "none")
    options = read_options()
    current_db = Path(str(options["database_path"]))
    backup_path = None
    actual_deleted = empty_purge_counts()
    estimated_deleted = preview["deleted"]
    needs_write = (
        int(estimated_deleted.get("total_datapoints") or 0) > 0
        or (cleanup_metadata and (
            int(estimated_deleted.get("states_meta") or 0) > 0
            or int(estimated_deleted.get("statistics_meta") or 0) > 0
        ))
    )

    if needs_write and bool(options.get("create_current_db_backup", True)):
        backup_path = backup_current_database(current_db)

    if needs_write:
        try:
            with open_db(current_db, readonly=False, timeout=30) as conn:
                conn.execute("PRAGMA busy_timeout = 30000")
                conn.execute("BEGIN IMMEDIATE")
                tables = table_names(conn)
                state_columns = column_names(conn, "states") if "states" in tables else []

                state_where, state_params, _ = state_where_for_entities(conn, tables, entity_ids, start, end)
                if state_where:
                    if (
                        "attributes_id" in state_columns
                        and "state_attributes" in tables
                        and "attributes_id" in column_names(conn, "state_attributes")
                    ):
                        conn.execute("CREATE TEMP TABLE IF NOT EXISTS purge_attribute_ids (attributes_id PRIMARY KEY)")
                        conn.execute("DELETE FROM purge_attribute_ids")
                        conn.execute(
                            f"""
                            INSERT OR IGNORE INTO purge_attribute_ids(attributes_id)
                            SELECT DISTINCT attributes_id
                            FROM states
                            WHERE {state_where} AND attributes_id IS NOT NULL
                            """,
                            state_params,
                        )
                    cursor = conn.execute(f"DELETE FROM states WHERE {state_where}", state_params)
                    actual_deleted["states"] = max(0, int(cursor.rowcount or 0))

                    if (
                        "attributes_id" in state_columns
                        and "state_attributes" in tables
                        and "attributes_id" in column_names(conn, "state_attributes")
                    ):
                        cursor = conn.execute(
                            """
                            DELETE FROM state_attributes
                            WHERE attributes_id IN (SELECT attributes_id FROM purge_attribute_ids)
                            AND NOT EXISTS (
                                SELECT 1
                                FROM states
                                WHERE states.attributes_id = state_attributes.attributes_id
                            )
                            """
                        )
                        actual_deleted["state_attributes"] = max(0, int(cursor.rowcount or 0))
                        conn.execute("DELETE FROM purge_attribute_ids")

                for table in STATISTICS_TABLES:
                    statistics_where, statistics_params, _ = statistics_where_for_entities(conn, tables, table, entity_ids, start, end)
                    if statistics_where:
                        cursor = conn.execute(
                            f"DELETE FROM {quote_identifier(table)} WHERE {statistics_where}",
                            statistics_params,
                        )
                        actual_deleted[table] = max(0, int(cursor.rowcount or 0))

                if cleanup_metadata:
                    metadata_deleted = cleanup_empty_entity_metadata(conn, tables, entity_ids)
                    actual_deleted["states_meta"] = metadata_deleted["states_meta"]
                    actual_deleted["statistics_meta"] = metadata_deleted["statistics_meta"]

                actual_deleted["total_datapoints"] = (
                    actual_deleted["states"]
                    + actual_deleted["statistics"]
                    + actual_deleted["statistics_short_term"]
                )
                conn.commit()
        except sqlite3.DatabaseError as err:
            raise AppError(f"Entity history could not be purged: {err}") from err

    maintenance_result = run_purge_database_maintenance(current_db, maintenance)

    current_analysis = analyze_database(current_db)
    remember_analysis(current_db, current_analysis)
    preview.update(
        {
            "backup_path": backup_path,
            "deleted": actual_deleted,
            "maintenance_result": maintenance_result,
            "current_database": current_analysis,
            "restart_recommended": needs_write or maintenance != "none",
        }
    )
    preview["report"] = write_purge_report(payload, preview, job_id)
    return preview


def purge_entity_history(entity_id: str) -> dict[str, Any]:
    return purge_entity_histories({"entity_ids": [entity_id]})


def candidate_score(member_name: str) -> int:
    normalized = member_name.replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    if basename == "home-assistant_v2.db":
        return 0
    if basename.endswith(".db") and "home-assistant" in basename:
        return 1
    if basename.endswith(".sqlite") or basename.endswith(".sqlite3"):
        return 3
    if basename.endswith(".db"):
        return 4
    return 100


def looks_like_nested_archive(member_name: str) -> bool:
    normalized = member_name.lower()
    return normalized.endswith((".tar", ".tar.gz", ".tgz", ".backup"))


def extract_database(upload_path: Path, target_path: Path, original_name: str, cancel_callback: Any = None) -> dict[str, Any]:
    check_cancel(cancel_callback)
    if is_sqlite_file(upload_path):
        copy_path(upload_path, target_path, cancel_callback)
        return {"kind": "sqlite", "selected_member": original_name}

    candidates: list[tuple[int, Path, str]] = []

    def scan_archive(path: Path, trail: str, depth: int, temp_root: Path) -> None:
        check_cancel(cancel_callback)
        if depth > 3:
            return
        if not tarfile.is_tarfile(path):
            return
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                check_cancel(cancel_callback)
                if not member.isfile():
                    continue
                member_name = f"{trail}/{member.name}" if trail else member.name
                score = candidate_score(member.name)
                if score < 100:
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    candidate_path = temp_root / f"candidate_{len(candidates)}.db"
                    copy_stream(extracted, candidate_path, cancel_callback)
                    if is_sqlite_file(candidate_path):
                        candidates.append((score, candidate_path, member_name))
                elif looks_like_nested_archive(member.name):
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    nested_path = temp_root / f"nested_{depth}_{len(candidates)}.tar"
                    copy_stream(extracted, nested_path, cancel_callback)
                    scan_archive(nested_path, member_name, depth + 1, temp_root)

    with tempfile.TemporaryDirectory(dir=str(TMP_DIR)) as temp_dir:
        scan_archive(upload_path, "", 0, Path(temp_dir))
        if not candidates:
            raise AppError("No Home Assistant SQLite database was found in the uploaded file.")

        candidates.sort(key=lambda item: (item[0], item[2]))
        _, selected_path, selected_member = candidates[0]
        copy_path(selected_path, target_path, cancel_callback)

    return {"kind": "archive", "selected_member": selected_member}


def write_source_meta(meta: dict[str, Any]) -> None:
    with SOURCE_META.open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True, default=json_default)


def read_source_meta() -> dict[str, Any] | None:
    if not SOURCE_META.exists():
        return None
    try:
        with SOURCE_META.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def cache_source_file(
    source_path: Path,
    original_name: str,
    source_kind: str,
    copy_original: bool,
    original_path: str | None = None,
    cancel_callback: Any = None,
) -> dict[str, Any]:
    working_db = TMP_DIR / f"source_{int(time.time())}.db"
    staged_original = TMP_DIR / f"source_original_{int(time.time())}_{uuid.uuid4().hex}"
    try:
        extract_info = extract_database(source_path, working_db, original_name, cancel_callback)
        analysis = analyze_database_for_cache(working_db)
        if not analysis["sqlite_header"]:
            raise AppError("The extracted file is not a SQLite database.")

        analysis_for_cache = copy.deepcopy(analysis)
        analysis_for_cache["path"] = str(SOURCE_DB)
        meta = {
            "cached_at": now_iso(),
            "source_kind": source_kind,
            "original_name": original_name,
            "original_path": original_path,
            "extract": extract_info,
            "analysis": analysis_for_cache,
            "entities_count": int(analysis.get("entities_count") or 0),
        }
        if copy_original:
            copy_path(source_path, staged_original, cancel_callback)

        check_cancel(cancel_callback)
        cleanup_source_database_files()
        if copy_original:
            staged_original.replace(SOURCE_ORIGINAL)
        else:
            try:
                SOURCE_ORIGINAL.unlink()
            except FileNotFoundError:
                pass
        working_db.replace(SOURCE_DB)
        meta["analysis"]["sidecars"] = database_sidecar_files(SOURCE_DB)
        meta["analysis"]["diagnostics"] = build_database_diagnostics(meta["analysis"])
        remember_analysis(SOURCE_DB, meta["analysis"])
        write_source_meta(meta)
        return {"meta": meta, "entities_count": meta["entities_count"], "entities_omitted": True}
    finally:
        try:
            working_db.unlink()
        except OSError:
            pass
        try:
            staged_original.unlink()
        except OSError:
            pass


def copy_recovery_sidecars(
    source_path: Path,
    destination_db: Path = SOURCE_DB,
    cancel_callback: Any = None,
    prefer_link: bool = False,
) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    destinations = {
        "wal": Path(f"{destination_db}-wal"),
        "shm": Path(f"{destination_db}-shm"),
        "journal": Path(f"{destination_db}-journal"),
    }
    for kind, sidecar in matching_corrupt_sidecars(source_path).items():
        destination = destinations[kind]
        storage = link_or_copy_path(sidecar, destination, cancel_callback, prefer_link=prefer_link)
        copied[kind] = {
            "source": str(sidecar),
            "destination": str(destination),
            "size_bytes": destination.stat().st_size,
            "storage": storage,
        }
    return copied


def sidecar_paths_for_database(database_path: Path) -> tuple[Path, Path, Path]:
    return (Path(f"{database_path}-wal"), Path(f"{database_path}-shm"), Path(f"{database_path}-journal"))


def remove_source_sidecars(database_path: Path = SOURCE_DB) -> None:
    for path in sidecar_paths_for_database(database_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def final_sidecar_info(copied_sidecars: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    final_paths = {
        "wal": Path(f"{SOURCE_DB}-wal"),
        "shm": Path(f"{SOURCE_DB}-shm"),
        "journal": Path(f"{SOURCE_DB}-journal"),
    }
    return {
        kind: {
            "source": info.get("source"),
            "destination": str(final_paths[kind]),
            "size_bytes": info.get("size_bytes"),
            "storage": info.get("storage"),
        }
        for kind, info in copied_sidecars.items()
    }


def cache_corrupt_database(file_id: str, cancel_callback: Any = None) -> dict[str, Any]:
    source_path = resolve_corrupt_database(file_id)
    try:
        source_size = source_path.stat().st_size
    except OSError:
        source_size = 0
    prefer_link = source_size >= AUTOMATIC_DEEP_ANALYSIS_MAX_BYTES
    with tempfile.TemporaryDirectory(dir=str(TMP_DIR)) as temp_dir:
        staged_db = Path(temp_dir) / "source.db"
        cache_storage = link_or_copy_path(source_path, staged_db, cancel_callback, prefer_link=prefer_link)
        copied_sidecars = copy_recovery_sidecars(source_path, staged_db, cancel_callback, prefer_link=prefer_link)

        analysis = analyze_database_for_cache(staged_db)
        warnings: list[str] = []
        used_sidecars = copied_sidecars
        if copied_sidecars and not analysis.get("readable", False):
            warnings.append("Quelle war mit WAL/SHM-Sidecars nicht lesbar. Es wurde automatisch ohne Sidecars erneut versucht.")
            remove_source_sidecars(staged_db)
            used_sidecars = {}
            analysis = analyze_database_for_cache(staged_db)

        if not analysis["sqlite_header"]:
            raise AppError("Selected corrupt database is not a SQLite database.")
        if not analysis.get("readable", analysis.get("ok", False)):
            raise AppError(analysis.get("error") or "Selected corrupt database is not readable enough for rescue.")

        analysis_for_cache = copy.deepcopy(analysis)
        analysis_for_cache["path"] = str(SOURCE_DB)
        meta = {
            "cached_at": now_iso(),
            "source_kind": "corrupt_database",
            "original_name": source_path.name,
            "original_path": str(source_path),
            "cache_storage": cache_storage,
            "recovery_sidecars": final_sidecar_info(used_sidecars),
            "recovery_warnings": warnings,
            "analysis": analysis_for_cache,
            "entities_count": int(analysis.get("entities_count") or 0),
        }

        check_cancel(cancel_callback)
        cleanup_source_database_files(include_meta=True)
        staged_db.replace(SOURCE_DB)
        for kind, info in used_sidecars.items():
            Path(str(info["destination"])).replace(Path(f"{SOURCE_DB}-{kind}"))
        meta["analysis"]["sidecars"] = database_sidecar_files(SOURCE_DB)
        meta["analysis"]["diagnostics"] = build_database_diagnostics(meta["analysis"])
        remember_analysis(SOURCE_DB, meta["analysis"])
        write_source_meta(meta)
        return {"meta": meta, "entities_count": meta["entities_count"], "entities_omitted": True}


def handle_uploaded_file(upload_path: Path, original_name: str, cancel_callback: Any = None) -> dict[str, Any]:
    return cache_source_file(upload_path, original_name, "upload", copy_original=True, cancel_callback=cancel_callback)


def handle_device_backup_file(file_id: str, cancel_callback: Any = None) -> dict[str, Any]:
    backup_path = resolve_device_backup(file_id)
    return cache_source_file(
        backup_path,
        backup_path.name,
        "device_backup",
        copy_original=False,
        original_path=str(backup_path),
        cancel_callback=cancel_callback,
    )


def cache_status() -> dict[str, Any]:
    meta = read_source_meta()
    analysis = source_cache_analysis(meta)
    return {
        "has_cached_database": SOURCE_DB.exists(),
        "cache_dir": str(CACHE_DIR),
        "upload_dir": str(UPLOAD_DIR),
        "tmp_dir": str(TMP_DIR),
        "source_db": str(SOURCE_DB),
        "source_original": str(SOURCE_ORIGINAL) if SOURCE_ORIGINAL.exists() else None,
        "storage": storage_info(CACHE_DIR),
        "meta": meta,
        "analysis": analysis,
    }


def ensure_target_metadata(conn: sqlite3.Connection, entity_id: str) -> int | None:
    if "states_meta" not in table_names(conn):
        return None
    columns = column_names(conn, "states_meta")
    if "metadata_id" not in columns or "entity_id" not in columns:
        return None

    row = conn.execute("SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,)).fetchone()
    if row:
        return int(row["metadata_id"])
    cursor = conn.execute("INSERT INTO states_meta (entity_id) VALUES (?)", (entity_id,))
    return int(cursor.lastrowid)


def existing_target_metadata(conn: sqlite3.Connection, entity_id: str) -> int | None:
    if "states_meta" not in table_names(conn):
        return None
    columns = column_names(conn, "states_meta")
    if "metadata_id" not in columns or "entity_id" not in columns:
        return None
    row = conn.execute("SELECT metadata_id FROM states_meta WHERE entity_id = ?", (entity_id,)).fetchone()
    return int(row["metadata_id"]) if row else None


def state_primary_key(columns: list[dict[str, Any]]) -> str | None:
    for column in columns:
        if column.get("pk"):
            return str(column["name"])
    return "state_id" if any(column["name"] == "state_id" for column in columns) else None


def attribute_primary_key(columns: list[dict[str, Any]]) -> str | None:
    for column in columns:
        if column.get("pk"):
            return str(column["name"])
    return "attributes_id" if any(column["name"] == "attributes_id" for column in columns) else None


def backup_current_database(current_db: Path) -> str:
    CURRENT_DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = CURRENT_DB_BACKUP_DIR / f"home-assistant_v2-{timestamp}.db"
    with sqlite3.connect(str(current_db), timeout=30) as source:
        with sqlite3.connect(str(backup_path)) as destination:
            source.backup(destination)
    return str(backup_path)


def create_current_database_snapshot() -> dict[str, Any]:
    options = read_options()
    current_db = Path(str(options["database_path"]))
    if not current_db.exists():
        raise AppError("Current Home Assistant database was not found.")
    if not is_sqlite_file(current_db):
        raise AppError("Current Home Assistant database path does not point to a SQLite database.")
    backup_path = backup_current_database(current_db)
    analysis = analyze_database(Path(backup_path))
    return {
        "snapshot_path": backup_path,
        "snapshot_analysis": analysis,
        "source_analysis": analyze_database(current_db),
    }


def checkpoint_current_database(mode: str = "PASSIVE") -> dict[str, Any]:
    normalized_mode = mode.upper()
    if normalized_mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise AppError("Unsupported WAL checkpoint mode.")
    options = read_options()
    current_db = Path(str(options["database_path"]))
    if not current_db.exists():
        raise AppError("Current Home Assistant database was not found.")
    if not is_sqlite_file(current_db):
        raise AppError("Current Home Assistant database path does not point to a SQLite database.")

    before = analyze_database(current_db)
    row_payload = None
    with open_db(current_db, readonly=False, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        row = conn.execute(f"PRAGMA wal_checkpoint({normalized_mode})").fetchone()
        if row is not None:
            keys = row.keys()
            row_payload = dict(row) if keys else list(row)
    after = analyze_database(current_db)
    return {
        "mode": normalized_mode,
        "checkpoint": row_payload,
        "before": before,
        "after": after,
        "restart_recommended": not bool(after.get("ok")),
    }


def list_current_db_backups(offset: int = 0, limit: int = 100) -> dict[str, Any]:
    CURRENT_DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for path in CURRENT_DB_BACKUP_DIR.glob("*.db"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append(
            {
                "id": path.name,
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
    files.sort(key=lambda item: (item["modified"], item["name"]), reverse=True)
    page = paginate_items(files, offset, limit)
    return {
        "backup_dir": str(CURRENT_DB_BACKUP_DIR),
        "files": page["items"],
        "offset": page["offset"],
        "limit": page["limit"],
        "total": page["total"],
        "has_next": page["has_next"],
        "has_previous": page["has_previous"],
    }


def resolve_current_db_backup(backup_id: str) -> Path:
    safe_id = safe_artifact_id(backup_id)
    backup_root = CURRENT_DB_BACKUP_DIR.resolve()
    path = (CURRENT_DB_BACKUP_DIR / safe_id).resolve()
    try:
        path.relative_to(backup_root)
    except ValueError as err:
        raise AppError("Backup path is outside the current DB backup directory.") from err
    if not path.is_file():
        raise AppError("Selected current DB backup does not exist.")
    if not is_sqlite_file(path):
        raise AppError("Selected current DB backup is not a SQLite database.")
    return path


def restore_current_database_from_backup(backup_id: str) -> dict[str, Any]:
    options = read_options()
    current_db = Path(str(options["database_path"]))
    backup_path = resolve_current_db_backup(backup_id)
    backup_analysis = analyze_database(backup_path)
    if not backup_analysis["sqlite_header"] or not backup_analysis["ok"]:
        raise AppError("Selected current DB backup is not healthy enough for restore.")
    pre_restore_backup = backup_current_database(current_db) if current_db.exists() else None
    shutil.copy2(backup_path, current_db)
    return {
        "restored_from": str(backup_path),
        "pre_restore_backup": pre_restore_backup,
        "current_database": analyze_database(current_db),
        "restart_recommended": True,
    }


def ensure_writable_dir(path: Path, label: str) -> None:
    if not path.parent.exists():
        raise AppError(f"{label} parent directory does not exist: {path.parent}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise AppError(f"{label} directory could not be created: {err}") from err
    if not path.is_dir():
        raise AppError(f"{label} path is not a directory: {path}")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise AppError(f"{label} directory is not readable and writable: {path}")


def config_backup_dir(options: dict[str, Any] | None = None) -> Path:
    return configured_config_backup_dir(options or read_options())


def ensure_config_backup_dir(options: dict[str, Any] | None = None) -> Path:
    backup_dir = config_backup_dir(options)
    ensure_writable_dir(backup_dir, "Config backup")
    return backup_dir


def safe_config_relative_path(value: Any) -> Path:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        raise AppError("Config backup entry has an empty path.")
    relative_path = Path(text)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise AppError(f"Config backup entry has an unsafe path: {text}")
    return relative_path


def resolve_config_relative_path(value: Any) -> Path:
    relative_path = safe_config_relative_path(value)
    root = CONFIG_DIR.resolve()
    path = (CONFIG_DIR / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as err:
        raise AppError(f"Config path is outside the Home Assistant config directory: {relative_path}") from err
    return path


def sha256_file(path: Path, cancel_callback: Any = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            check_cancel(cancel_callback)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_stream(stream: Any, cancel_callback: Any = None) -> str:
    digest = hashlib.sha256()
    while True:
        check_cancel(cancel_callback)
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def normalize_config_backup_components(payload: dict[str, Any] | None) -> tuple[list[str], bool]:
    source = payload or {}
    include_secrets = bool(source.get("include_secrets", False))
    raw_components = source.get("components")
    if raw_components is None:
        components = list(DEFAULT_CONFIG_BACKUP_COMPONENTS)
    elif isinstance(raw_components, list):
        components = [str(item).strip() for item in raw_components if str(item).strip()]
    else:
        raise AppError("Config backup components need to be a list.")

    normalized: list[str] = []
    for component in components:
        if component not in CONFIG_BACKUP_COMPONENTS:
            raise AppError(f"Unknown config backup component: {component}")
        if CONFIG_BACKUP_COMPONENTS[component].get("sensitive") and not include_secrets:
            raise AppError("Secrets need explicit include_secrets confirmation.")
        if component not in normalized:
            normalized.append(component)

    if include_secrets and "secrets" not in normalized:
        normalized.append("secrets")
    if not normalized:
        raise AppError("At least one config backup component needs to be selected.")
    return normalized, include_secrets


def collect_config_backup_files(components: list[str], cancel_callback: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not CONFIG_DIR.exists():
        raise AppError(f"Home Assistant config directory does not exist: {CONFIG_DIR}")
    root = CONFIG_DIR.resolve()
    files_by_path: dict[str, dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []

    def add_file(path: Path, component: str) -> None:
        check_cancel(cancel_callback)
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
            relative_path = path.relative_to(CONFIG_DIR).as_posix()
            stat = resolved.stat()
        except (OSError, ValueError):
            return
        if not resolved.is_file():
            return
        item = files_by_path.setdefault(
            relative_path,
            {
                "path": resolved,
                "relative_path": relative_path,
                "components": [],
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "mode": stat.st_mode & 0o777,
            },
        )
        if component not in item["components"]:
            item["components"].append(component)

    for component in components:
        component_config = CONFIG_BACKUP_COMPONENTS[component]
        for pattern in component_config["patterns"]:
            matches = sorted(CONFIG_DIR.glob(pattern)) if any(token in pattern for token in "*?[") else [CONFIG_DIR / pattern]
            existing = [path for path in matches if path.exists()]
            if not existing:
                missing.append({"component": component, "pattern": pattern})
                continue
            for path in existing:
                check_cancel(cancel_callback)
                if path.is_dir():
                    for child in sorted(path.rglob("*")):
                        if child.is_file():
                            add_file(child, component)
                elif path.is_file():
                    add_file(path, component)

    return sorted(files_by_path.values(), key=lambda item: item["relative_path"]), missing


def component_labels(components: list[str]) -> list[str]:
    return [str(CONFIG_BACKUP_COMPONENTS.get(component, {}).get("label") or component) for component in components]


def write_config_backup_archive(
    files: list[dict[str, Any]],
    components: list[str],
    include_secrets: bool,
    *,
    reason: str,
    missing: list[dict[str, Any]] | None = None,
    cancel_callback: Any = None,
) -> dict[str, Any]:
    backup_dir = ensure_config_backup_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = "ha-config-pre-restore" if reason == "pre_restore" else "ha-config"
    archive_id = f"{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}{CONFIG_BACKUP_EXTENSION}"
    archive_path = backup_dir / archive_id
    temp_path = backup_dir / f".{archive_id}.tmp"

    total_size = sum(int(item.get("size_bytes") or 0) for item in files)
    try:
        if shutil.disk_usage(backup_dir).free < total_size + 1024 * 1024:
            raise AppError("Not enough free space in the config backup directory.")
    except OSError as err:
        raise AppError(f"Could not check free config backup space: {err}") from err

    manifest_files: list[dict[str, Any]] = []
    for item in files:
        check_cancel(cancel_callback)
        path = Path(item["path"])
        manifest_files.append(
            {
                "path": item["relative_path"],
                "size_bytes": item["size_bytes"],
                "modified": item["modified"],
                "sha256": sha256_file(path, cancel_callback),
                "components": item.get("components") or [],
                "mode": item.get("mode"),
            }
        )

    manifest = {
        "id": archive_id,
        "created_at": now_iso(),
        "app": "backup_db_restore",
        "app_version": APP_VERSION,
        "reason": reason,
        "config_dir": str(CONFIG_DIR),
        "components": components,
        "component_labels": component_labels(components),
        "include_secrets": include_secrets,
        "missing": missing or [],
        "files": manifest_files,
        "file_count": len(manifest_files),
        "total_size_bytes": total_size,
    }

    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            for item in files:
                check_cancel(cancel_callback)
                path = Path(item["path"])
                stat = path.stat()
                tar_info = tarfile.TarInfo(f"config/{item['relative_path']}")
                tar_info.size = stat.st_size
                tar_info.mtime = stat.st_mtime
                tar_info.mode = int(item.get("mode") or (stat.st_mode & 0o777))
                with path.open("rb") as handle:
                    archive.addfile(tar_info, handle)

            manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True, default=json_default).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            manifest_info.mtime = time.time()
            manifest_info.mode = 0o644
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        temp_path.replace(archive_path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise

    return {
        "id": archive_id,
        "path": str(archive_path),
        "manifest": manifest,
        "storage": storage_info(backup_dir),
    }


def create_config_backup(payload: dict[str, Any] | None = None, cancel_callback: Any = None) -> dict[str, Any]:
    components, include_secrets = normalize_config_backup_components(payload)
    files, missing = collect_config_backup_files(components, cancel_callback)
    if not files:
        raise AppError("No matching Home Assistant config files were found for the selected components.")
    return write_config_backup_archive(
        files,
        components,
        include_secrets,
        reason="manual",
        missing=missing,
        cancel_callback=cancel_callback,
    )


def list_config_backups(offset: int = 0, limit: int = 100) -> dict[str, Any]:
    backup_dir = ensure_config_backup_dir()
    files: list[dict[str, Any]] = []
    for path in backup_dir.glob(f"*{CONFIG_BACKUP_EXTENSION}"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
            manifest = read_config_backup_manifest_from_path(path)
        except (OSError, tarfile.TarError, json.JSONDecodeError, AppError):
            manifest = {}
            try:
                stat = path.stat()
            except OSError:
                continue
        files.append(
            {
                "id": path.name,
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "created_at": manifest.get("created_at"),
                "reason": manifest.get("reason"),
                "file_count": manifest.get("file_count"),
                "total_size_bytes": manifest.get("total_size_bytes"),
                "component_labels": manifest.get("component_labels") or [],
                "include_secrets": bool(manifest.get("include_secrets", False)),
            }
        )
    files.sort(key=lambda item: (item.get("created_at") or item["modified"], item["name"]), reverse=True)
    page = paginate_items(files, offset, limit)
    return {
        "backup_dir": str(backup_dir),
        "config_dir": str(CONFIG_DIR),
        "storage": storage_info(backup_dir),
        "files": page["items"],
        "offset": page["offset"],
        "limit": page["limit"],
        "total": page["total"],
        "has_next": page["has_next"],
        "has_previous": page["has_previous"],
    }


def resolve_config_backup(backup_id: str) -> Path:
    safe_id = safe_artifact_id(backup_id)
    backup_dir = ensure_config_backup_dir()
    backup_root = backup_dir.resolve()
    path = (backup_dir / safe_id).resolve()
    try:
        path.relative_to(backup_root)
    except ValueError as err:
        raise AppError("Config backup path is outside the config backup directory.") from err
    if not path.is_file():
        raise AppError("Selected config backup does not exist.")
    if not path.name.endswith(CONFIG_BACKUP_EXTENSION):
        raise AppError("Selected config backup is not a tar.gz archive.")
    return path


def read_config_backup_manifest_from_path(path: Path) -> dict[str, Any]:
    with tarfile.open(path, "r:*") as archive:
        try:
            member = archive.getmember("manifest.json")
        except KeyError as err:
            raise AppError("Config backup has no manifest.") from err
        extracted = archive.extractfile(member)
        if extracted is None:
            raise AppError("Config backup manifest could not be read.")
        manifest = json.loads(extracted.read().decode("utf-8"))
    if not isinstance(manifest, dict):
        raise AppError("Config backup manifest is invalid.")
    manifest.setdefault("id", path.name)
    return manifest


def validate_config_backup_archive(path: Path, cancel_callback: Any = None) -> dict[str, Any]:
    with tarfile.open(path, "r:*") as archive:
        try:
            manifest_member = archive.getmember("manifest.json")
        except KeyError as err:
            raise AppError("Config backup upload has no manifest.") from err
        if not manifest_member.isfile():
            raise AppError("Config backup manifest is not a regular file.")
        extracted_manifest = archive.extractfile(manifest_member)
        if extracted_manifest is None:
            raise AppError("Config backup manifest could not be read.")
        try:
            manifest = json.loads(extracted_manifest.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise AppError(f"Config backup manifest is invalid: {err}") from err
        if not isinstance(manifest, dict):
            raise AppError("Config backup manifest needs to be an object.")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise AppError("Config backup manifest contains no files.")

        allowed_members = {"manifest.json"}
        for entry in files:
            if not isinstance(entry, dict):
                raise AppError("Config backup manifest contains an invalid file entry.")
            relative_path = safe_config_relative_path(entry.get("path")).as_posix()
            member_name = f"config/{relative_path}"
            allowed_members.add(member_name)
            try:
                member = archive.getmember(member_name)
            except KeyError as err:
                raise AppError(f"Config backup upload is missing archive member: {member_name}") from err
            if not member.isfile():
                raise AppError(f"Config backup member is not a regular file: {member_name}")
            if entry.get("size_bytes") is not None:
                try:
                    expected_size = int(entry["size_bytes"])
                except (TypeError, ValueError) as err:
                    raise AppError(f"Config backup member has an invalid size: {relative_path}") from err
                if expected_size != int(member.size):
                    raise AppError(f"Config backup member size mismatch: {relative_path}")
            expected_sha = entry.get("sha256")
            if expected_sha:
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise AppError(f"Config backup member could not be read: {member_name}")
                actual_sha = sha256_stream(extracted, cancel_callback)
                if actual_sha != expected_sha:
                    raise AppError(f"Config backup checksum mismatch: {relative_path}")

        for member in archive.getmembers():
            check_cancel(cancel_callback)
            if member.name not in allowed_members:
                raise AppError(f"Config backup contains an unexpected archive member: {member.name}")

    manifest.setdefault("id", path.name)
    return manifest


def config_backup_upload_name(original_name: str) -> str:
    normalized = Path(original_name).name.strip() or f"ha-config-upload-{uuid.uuid4().hex[:8]}{CONFIG_BACKUP_EXTENSION}"
    if normalized.endswith(".tgz"):
        normalized = f"{normalized[:-4]}{CONFIG_BACKUP_EXTENSION}"
    if not normalized.endswith(CONFIG_BACKUP_EXTENSION):
        raise AppError("Config backup upload needs to be a .tar.gz archive.")
    if not re.match(r"^[A-Za-z0-9_.-]+$", normalized) or normalized.startswith("."):
        normalized = f"ha-config-upload-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{CONFIG_BACKUP_EXTENSION}"
    return normalized


def unique_config_backup_path(file_name: str) -> Path:
    backup_dir = ensure_config_backup_dir()
    target = backup_dir / file_name
    if not target.exists():
        return target
    stem = file_name[: -len(CONFIG_BACKUP_EXTENSION)]
    for _ in range(20):
        candidate = backup_dir / f"{stem}-{uuid.uuid4().hex[:8]}{CONFIG_BACKUP_EXTENSION}"
        if not candidate.exists():
            return candidate
    raise AppError("Could not allocate a unique config backup upload name.")


def import_config_backup_upload(upload_path: Path, original_name: str, cancel_callback: Any = None) -> dict[str, Any]:
    manifest = validate_config_backup_archive(upload_path, cancel_callback)
    target_path = unique_config_backup_path(config_backup_upload_name(original_name))
    try:
        upload_path.replace(target_path)
    except OSError as err:
        raise AppError(f"Config backup upload could not be stored: {err}") from err
    manifest["id"] = target_path.name
    return {
        "id": target_path.name,
        "path": str(target_path),
        "manifest": manifest,
        "storage": storage_info(target_path.parent),
    }


def read_config_backup(backup_id: str) -> dict[str, Any]:
    backup_path = resolve_config_backup(backup_id)
    return {
        "id": backup_path.name,
        "path": str(backup_path),
        "manifest": read_config_backup_manifest_from_path(backup_path),
    }


def preview_config_backup_restore(backup_id: str) -> dict[str, Any]:
    backup = read_config_backup(backup_id)
    manifest = backup["manifest"]
    changes: list[dict[str, Any]] = []
    counts = {"same": 0, "changed": 0, "new": 0, "missing_backup_entry": 0, "conflict": 0}
    for entry in manifest.get("files") or []:
        relative_path = safe_config_relative_path(entry.get("path")).as_posix()
        target = resolve_config_relative_path(relative_path)
        item = {
            "path": relative_path,
            "backup_sha256": entry.get("sha256"),
            "backup_size_bytes": entry.get("size_bytes"),
            "status": "new",
            "current_sha256": None,
            "current_size_bytes": None,
        }
        if target.exists():
            if not target.is_file():
                item["status"] = "conflict"
            else:
                item["current_sha256"] = sha256_file(target)
                try:
                    item["current_size_bytes"] = target.stat().st_size
                except OSError:
                    item["current_size_bytes"] = None
                item["status"] = "same" if item["current_sha256"] == entry.get("sha256") else "changed"
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        changes.append(item)

    return {
        "backup": backup,
        "changes": changes,
        "counts": counts,
        "restart_recommended": True,
        "storage": storage_info(config_backup_dir()),
    }


def create_pre_restore_config_backup(manifest: dict[str, Any], cancel_callback: Any = None) -> dict[str, Any] | None:
    files: list[dict[str, Any]] = []
    for entry in manifest.get("files") or []:
        relative_path = safe_config_relative_path(entry.get("path")).as_posix()
        path = resolve_config_relative_path(relative_path)
        check_cancel(cancel_callback)
        if not path.exists() or not path.is_file():
            continue
        stat = path.stat()
        files.append(
            {
                "path": path,
                "relative_path": relative_path,
                "components": ["pre_restore"],
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "mode": stat.st_mode & 0o777,
            }
        )
    if not files:
        return None
    return write_config_backup_archive(
        files,
        ["pre_restore"],
        include_secrets=any(item["relative_path"] == "secrets.yaml" for item in files),
        reason="pre_restore",
        cancel_callback=cancel_callback,
    )


def restore_config_backup(backup_id: str, cancel_callback: Any = None) -> dict[str, Any]:
    backup_path = resolve_config_backup(backup_id)
    manifest = read_config_backup_manifest_from_path(backup_path)
    preview = preview_config_backup_restore(backup_id)
    if preview["counts"].get("conflict"):
        raise AppError("Config restore has path conflicts. Preview the backup before restoring.")

    pre_restore_backup = create_pre_restore_config_backup(manifest, cancel_callback)
    restored: list[dict[str, Any]] = []
    with tarfile.open(backup_path, "r:*") as archive:
        for entry in manifest.get("files") or []:
            check_cancel(cancel_callback)
            relative_path = safe_config_relative_path(entry.get("path")).as_posix()
            target = resolve_config_relative_path(relative_path)
            member_name = f"config/{relative_path}"
            try:
                member = archive.getmember(member_name)
            except KeyError as err:
                raise AppError(f"Config backup is missing archive member: {member_name}") from err
            if not member.isfile():
                raise AppError(f"Config backup member is not a regular file: {member_name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AppError(f"Config backup member could not be read: {member_name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
            try:
                copy_stream(extracted, temp_path, cancel_callback)
                if entry.get("sha256") and sha256_file(temp_path, cancel_callback) != entry.get("sha256"):
                    raise AppError(f"Config backup checksum mismatch: {relative_path}")
                if entry.get("mode") is not None:
                    try:
                        temp_path.chmod(int(entry["mode"]))
                    except (OSError, TypeError, ValueError):
                        pass
                temp_path.replace(target)
            except Exception:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
                raise
            restored.append({"path": relative_path, "size_bytes": entry.get("size_bytes"), "sha256": entry.get("sha256")})

    return {
        "restored_from": str(backup_path),
        "pre_restore_backup": pre_restore_backup,
        "restored": restored,
        "restored_count": len(restored),
        "restart_recommended": True,
    }


def compact_report_result(value: Any) -> Any:
    if isinstance(value, list):
        return [compact_report_result(item) for item in value]
    if not isinstance(value, dict):
        return value

    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key == "entities" and isinstance(item, list):
            compact["entities_count"] = len(item)
            compact["entities_omitted"] = True
            continue
        compact[key] = compact_report_result(item)
    return compact


def write_import_report(payload: dict[str, Any], result: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_id = f"{timestamp}-{uuid.uuid4().hex[:8]}.json"
    safe_payload = {
        "source_entity_id": payload.get("source_entity_id"),
        "target_entity_id": payload.get("target_entity_id"),
        "dry_run": bool(payload.get("dry_run", True)),
        "include_statistics": bool(payload.get("include_statistics", False)),
        "duplicate_strategy": payload.get("duplicate_strategy", "skip"),
        "start": payload.get("start"),
        "end": payload.get("end"),
    }
    report = {
        "id": report_id,
        "kind": "import",
        "created_at": now_iso(),
        "job_id": job_id,
        "payload": safe_payload,
        "result": result,
    }
    with (REPORT_DIR / report_id).open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=json_default)
    return {"id": report_id, "path": str(REPORT_DIR / report_id)}


def write_purge_report(payload: dict[str, Any], result: dict[str, Any], job_id: str | None = None) -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_id = f"purge-{timestamp}-{uuid.uuid4().hex[:8]}.json"
    safe_payload = {
        "entity_ids": list(result.get("entity_ids") or payload.get("entity_ids") or []),
        "start": (result.get("time_range") or {}).get("start") or payload.get("start"),
        "end": (result.get("time_range") or {}).get("end") or payload.get("end"),
        "cleanup_metadata": bool(result.get("cleanup_metadata") or payload.get("cleanup_metadata", False)),
        "maintenance": result.get("maintenance") or payload.get("maintenance") or "none",
    }
    report = {
        "id": report_id,
        "kind": "purge",
        "created_at": now_iso(),
        "job_id": job_id,
        "payload": safe_payload,
        "result": compact_report_result(result),
    }
    with (REPORT_DIR / report_id).open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=json_default)
    return {"id": report_id, "path": str(REPORT_DIR / report_id)}


def list_import_reports(offset: int = 0, limit: int = 50) -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    for path in REPORT_DIR.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
            stat = path.stat()
        except (OSError, json.JSONDecodeError):
            continue
        payload = report.get("payload") or {}
        result = report.get("result") or {}
        kind = str(report.get("kind") or "import")
        deleted = result.get("deleted") or {}
        purge_entity_ids = payload.get("entity_ids") if isinstance(payload.get("entity_ids"), list) else []
        purge_source = ", ".join(str(entity_id) for entity_id in purge_entity_ids[:2])
        if len(purge_entity_ids) > 2:
            purge_source = f"{purge_source}, ..."
        reports.append(
            {
                "id": path.name,
                "kind": kind,
                "created_at": report.get("created_at"),
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_entity_id": payload.get("source_entity_id") or purge_source,
                "target_entity_id": payload.get("target_entity_id") or ("Purge" if kind == "purge" else None),
                "dry_run": payload.get("dry_run"),
                "states_inserted": result.get("inserted"),
                "states_skipped": result.get("skipped"),
                "states_replaced": result.get("replaced"),
                "statistics_inserted": (result.get("statistics") or {}).get("inserted"),
                "states_deleted": deleted.get("states"),
                "statistics_deleted": deleted.get("statistics"),
                "statistics_short_term_deleted": deleted.get("statistics_short_term"),
                "metadata_deleted": int(deleted.get("states_meta") or 0) + int(deleted.get("statistics_meta") or 0),
                "backup_path": result.get("backup_path"),
            }
        )
    reports.sort(key=lambda item: (item.get("created_at") or "", item["id"]), reverse=True)
    page = paginate_items(reports, offset, limit)
    return {
        "reports": page["items"],
        "offset": page["offset"],
        "limit": page["limit"],
        "total": page["total"],
        "has_next": page["has_next"],
        "has_previous": page["has_previous"],
    }


def read_import_report(report_id: str) -> dict[str, Any]:
    safe_id = safe_artifact_id(report_id)
    report_root = REPORT_DIR.resolve()
    path = (REPORT_DIR / safe_id).resolve()
    try:
        path.relative_to(report_root)
    except ValueError as err:
        raise AppError("Report path is outside the report directory.") from err
    if not path.is_file():
        raise AppError("Report was not found.", HTTPStatus.NOT_FOUND)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class HistoryImporter:
    def __init__(
        self,
        source_db: Path,
        target_db: Path,
        source_entity: str,
        target_entity: str,
        dry_run: bool,
        include_statistics: bool = False,
        start: Any = None,
        end: Any = None,
        duplicate_strategy: str = "skip",
        progress_callback: Any = None,
        cancel_callback: Any = None,
    ) -> None:
        self.source_db = source_db
        self.target_db = target_db
        self.source_entity = source_entity
        self.target_entity = target_entity
        self.dry_run = dry_run
        self.include_statistics = include_statistics
        self.start = start
        self.end = end
        self.duplicate_strategy = duplicate_strategy if duplicate_strategy in {"skip", "replace"} else "skip"
        self.progress_callback = progress_callback
        self.cancel_callback = cancel_callback
        self.attribute_map: dict[int, int | None] = {}
        self.source_warnings: list[str] = []

    def check_cancelled(self) -> None:
        check_cancel(self.cancel_callback)

    def run(self) -> dict[str, Any]:
        self.check_cancelled()
        if not self.source_db.exists():
            raise AppError("No cached source database is available. Upload or cache a database first.")
        if not self.target_db.exists():
            raise AppError("Current Home Assistant database was not found.")
        if not ENTITY_ID_RE.match(self.source_entity):
            raise AppError("Source entity id is invalid.")
        if not ENTITY_ID_RE.match(self.target_entity):
            raise AppError("Target entity id is invalid.")

        if self.source_db == SOURCE_DB:
            source_analysis = source_cache_analysis(read_source_meta()) or analyze_database_for_cache(self.source_db)
        else:
            source_analysis = analyze_database_for_cache(self.source_db)
        target_analysis = cached_analyze_database(self.target_db)
        if not source_analysis["sqlite_header"] or not source_analysis.get("readable", False):
            raise AppError("Source database is not readable enough for import.")
        if not source_analysis.get("ok", False):
            self.source_warnings = list(source_analysis.get("read_errors") or [])
        if not target_analysis["sqlite_header"] or not target_analysis["ok"]:
            raise AppError("Current database is not healthy enough for import.")
        self.check_cancelled()

        with open_db(self.source_db, readonly=True) as source_conn:
            if not database_entity_exists(self.source_db, self.source_entity):
                raise AppError("Source entity was not found in the cached database.")
            self.check_cancelled()

            if self.dry_run:
                with open_db(self.target_db, readonly=True) as target_conn:
                    result = self.copy_states(source_conn, target_conn, None)
                    if self.include_statistics:
                        result["statistics"] = self.copy_statistics(source_conn, target_conn, None)
                    return result

            backup_path = None
            options = read_options()
            if bool(options.get("create_current_db_backup", True)):
                backup_path = backup_current_database(self.target_db)
            self.check_cancelled()

            with open_db(self.target_db, readonly=False, timeout=30) as target_conn:
                target_conn.execute("PRAGMA busy_timeout = 30000")
                target_conn.execute("BEGIN IMMEDIATE")
                try:
                    self.check_cancelled()
                    target_metadata_id = ensure_target_metadata(target_conn, self.target_entity)
                    result = self.copy_states(source_conn, target_conn, target_metadata_id)
                    if self.include_statistics:
                        result["statistics"] = self.copy_statistics(source_conn, target_conn, None)
                    target_conn.commit()
                except Exception:
                    target_conn.rollback()
                    raise

            result["backup_path"] = backup_path
            return result

    def copy_states(
        self,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        target_metadata_id: int | None,
    ) -> dict[str, Any]:
        source_tables = table_names(source_conn)
        target_tables = table_names(target_conn)
        if "states" not in source_tables or "states" not in target_tables:
            raise AppError("Source and target database both need a states table.")

        source_state_columns = table_columns(source_conn, "states")
        target_state_columns = table_columns(target_conn, "states")
        source_state_names = [str(column["name"]) for column in source_state_columns]
        target_state_names = [str(column["name"]) for column in target_state_columns]
        target_state_pk = state_primary_key(target_state_columns)

        join_sql, where_sql, params = source_entity_join_and_where(source_conn, self.source_entity)
        query_params = list(params)
        where_parts = [where_sql]
        time_column = state_time_column_name(source_state_names)
        if self.start is not None or self.end is not None:
            if not time_column:
                raise AppError("Source states table has no supported time column for a time range import.")
            start_value = datetime_for_column(self.start, time_column)
            end_value = datetime_for_column(self.end, time_column)
            if start_value is not None:
                where_parts.append(f"s.{quote_identifier(time_column)} >= ?")
                query_params.append(start_value)
            if end_value is not None:
                where_parts.append(f"s.{quote_identifier(time_column)} <= ?")
                query_params.append(end_value)
        where_clause = " AND ".join(where_parts)
        order_sql = state_order_clause(source_state_names)
        count_sql = f"SELECT COUNT(*) FROM states s {join_sql} WHERE {where_clause}"
        total_rows = int(source_conn.execute(count_sql, query_params).fetchone()[0])
        source_sql = f"SELECT s.* FROM states s {join_sql} WHERE {where_clause} ORDER BY {order_sql}"

        inserted = 0
        skipped = 0
        replaced = 0
        scanned = 0
        first_imported = None
        last_imported = None
        read_errors: list[str] = []

        for source_row in query_rows_best_effort(source_conn, source_sql, query_params, read_errors, "states"):
            scanned += 1
            if scanned == 1 or scanned % 1000 == 0:
                self.check_cancelled()
            duplicate = self.is_duplicate(target_conn, source_row, target_metadata_id, target_state_names)
            if duplicate:
                if self.duplicate_strategy == "skip":
                    skipped += 1
                    continue
                replaced += self.delete_duplicate(target_conn, source_row, target_metadata_id, target_state_names)

            timestamp = self.row_time(source_row, source_state_names)
            if first_imported is None:
                first_imported = timestamp
            last_imported = timestamp

            if self.dry_run:
                inserted += 1
                if self.progress_callback and scanned % 1000 == 0:
                    percent = 15 + int((scanned / max(total_rows, 1)) * 55)
                    self.progress_callback(min(percent, 70), f"States geprueft: {scanned}/{total_rows}")
                continue

            insert_columns, values = self.build_insert(
                source_conn,
                target_conn,
                source_row,
                target_state_names,
                target_state_pk,
                target_metadata_id,
            )
            placeholders = ", ".join("?" for _ in values)
            column_sql = ", ".join(quote_identifier(column) for column in insert_columns)
            target_conn.execute(f"INSERT INTO states ({column_sql}) VALUES ({placeholders})", values)
            inserted += 1
            if self.progress_callback and scanned % 1000 == 0:
                percent = 15 + int((scanned / max(total_rows, 1)) * 55)
                self.progress_callback(min(percent, 70), f"States verarbeitet: {scanned}/{total_rows}")

        return {
            "dry_run": self.dry_run,
            "source_entity": self.source_entity,
            "target_entity": self.target_entity,
            "scanned": scanned,
            "inserted": inserted,
            "skipped": skipped,
            "replaced": replaced,
            "duplicate_strategy": self.duplicate_strategy,
            "start": self.start,
            "end": self.end,
            "source_warnings": self.source_warnings,
            "read_errors": read_errors,
            "partial": bool(self.source_warnings or read_errors),
            "first_imported": first_imported,
            "last_imported": last_imported,
            "backup_path": None,
        }

    def row_time(self, row: sqlite3.Row, columns: list[str]) -> str | None:
        for column in ("last_updated_ts", "last_changed_ts", "last_updated", "last_changed"):
            if column in columns:
                return format_db_time(row[column])
        return None

    def is_duplicate(
        self,
        target_conn: sqlite3.Connection,
        source_row: sqlite3.Row,
        target_metadata_id: int | None,
        target_columns: list[str],
    ) -> bool:
        source_keys = set(source_row.keys())
        time_column = None
        for column in ("last_updated_ts", "last_changed_ts", "last_updated", "last_changed"):
            if column in target_columns and column in source_keys:
                time_column = column
                break
        if not time_column:
            return False

        if "metadata_id" in target_columns:
            metadata_id = target_metadata_id
            if metadata_id is None:
                metadata_id = existing_target_metadata(target_conn, self.target_entity)
            if metadata_id is None:
                return False
            sql = f"""
                SELECT 1
                FROM states
                WHERE metadata_id = ? AND {quote_identifier(time_column)} = ?
                LIMIT 1
            """
            row = target_conn.execute(sql, (metadata_id, source_row[time_column])).fetchone()
            return row is not None

        if "entity_id" in target_columns:
            sql = f"""
                SELECT 1
                FROM states
                WHERE entity_id = ? AND {quote_identifier(time_column)} = ?
                LIMIT 1
            """
            row = target_conn.execute(sql, (self.target_entity, source_row[time_column])).fetchone()
            return row is not None

        return False

    def delete_duplicate(
        self,
        target_conn: sqlite3.Connection,
        source_row: sqlite3.Row,
        target_metadata_id: int | None,
        target_columns: list[str],
    ) -> int:
        if self.dry_run:
            return 1
        source_keys = set(source_row.keys())
        time_column = None
        for column in ("last_updated_ts", "last_changed_ts", "last_updated", "last_changed"):
            if column in target_columns and column in source_keys:
                time_column = column
                break
        if not time_column:
            return 0

        if "metadata_id" in target_columns:
            metadata_id = target_metadata_id
            if metadata_id is None:
                metadata_id = existing_target_metadata(target_conn, self.target_entity)
            if metadata_id is None:
                return 0
            cursor = target_conn.execute(
                f"DELETE FROM states WHERE metadata_id = ? AND {quote_identifier(time_column)} = ?",
                (metadata_id, source_row[time_column]),
            )
            return int(cursor.rowcount or 0)

        if "entity_id" in target_columns:
            cursor = target_conn.execute(
                f"DELETE FROM states WHERE entity_id = ? AND {quote_identifier(time_column)} = ?",
                (self.target_entity, source_row[time_column]),
            )
            return int(cursor.rowcount or 0)

        return 0

    def build_insert(
        self,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        source_row: sqlite3.Row,
        target_columns: list[str],
        target_state_pk: str | None,
        target_metadata_id: int | None,
    ) -> tuple[list[str], list[Any]]:
        insert_columns: list[str] = []
        values: list[Any] = []
        source_keys = set(source_row.keys())

        for column in target_columns:
            if column == target_state_pk:
                continue
            if column == "metadata_id":
                value = target_metadata_id
            elif column == "entity_id":
                value = self.target_entity
            elif column == "attributes_id":
                value = self.copy_attribute(source_conn, target_conn, source_row["attributes_id"]) if "attributes_id" in source_keys else None
            elif column == "old_state_id":
                value = None
            elif column in source_keys:
                value = source_row[column]
            else:
                continue
            insert_columns.append(column)
            values.append(value)

        if not insert_columns:
            raise AppError("No compatible state columns were found for import.")
        return insert_columns, values

    def copy_attribute(self, source_conn: sqlite3.Connection, target_conn: sqlite3.Connection, source_attribute_id: Any) -> int | None:
        if source_attribute_id is None:
            return None
        try:
            source_attribute_key = int(source_attribute_id)
        except (TypeError, ValueError):
            return None
        if source_attribute_key in self.attribute_map:
            return self.attribute_map[source_attribute_key]

        source_tables = table_names(source_conn)
        target_tables = table_names(target_conn)
        if "state_attributes" not in source_tables or "state_attributes" not in target_tables:
            self.attribute_map[source_attribute_key] = None
            return None

        source_columns = table_columns(source_conn, "state_attributes")
        target_columns = table_columns(target_conn, "state_attributes")
        source_names = [str(column["name"]) for column in source_columns]
        target_names = [str(column["name"]) for column in target_columns]
        source_pk = attribute_primary_key(source_columns)
        target_pk = attribute_primary_key(target_columns)
        if not source_pk or not target_pk:
            self.attribute_map[source_attribute_key] = None
            return None

        row = source_conn.execute(
            f"SELECT * FROM state_attributes WHERE {quote_identifier(source_pk)} = ?",
            (source_attribute_key,),
        ).fetchone()
        if row is None:
            self.attribute_map[source_attribute_key] = None
            return None

        if "hash" in source_names and "hash" in target_names and "shared_attrs" in source_names and "shared_attrs" in target_names:
            existing = target_conn.execute(
                f"SELECT {quote_identifier(target_pk)} AS target_attribute_id FROM state_attributes WHERE hash = ? AND shared_attrs = ? LIMIT 1",
                (row["hash"], row["shared_attrs"]),
            ).fetchone()
            if existing:
                mapped = int(existing["target_attribute_id"])
                self.attribute_map[source_attribute_key] = mapped
                return mapped

        insert_columns = [
            column
            for column in target_names
            if column in source_names and column != target_pk
        ]
        if not insert_columns:
            self.attribute_map[source_attribute_key] = None
            return None

        values = [row[column] for column in insert_columns]
        placeholders = ", ".join("?" for _ in values)
        column_sql = ", ".join(quote_identifier(column) for column in insert_columns)
        cursor = target_conn.execute(f"INSERT INTO state_attributes ({column_sql}) VALUES ({placeholders})", values)
        mapped = int(cursor.lastrowid)
        self.attribute_map[source_attribute_key] = mapped
        return mapped

    def source_statistics_meta(self, source_conn: sqlite3.Connection) -> tuple[sqlite3.Row | None, str | None, str | None]:
        if "statistics_meta" not in table_names(source_conn):
            return None, None, None
        columns = table_columns(source_conn, "statistics_meta")
        names = [str(column["name"]) for column in columns]
        pk = statistics_meta_primary_key(columns)
        if not pk or "statistic_id" not in names:
            return None, None, None
        row = source_conn.execute("SELECT * FROM statistics_meta WHERE statistic_id = ?", (self.source_entity,)).fetchone()
        return row, pk, pk

    def ensure_target_statistics_meta(
        self,
        source_meta_row: sqlite3.Row,
        source_meta_pk: str,
        target_conn: sqlite3.Connection,
    ) -> int:
        target_columns = table_columns(target_conn, "statistics_meta")
        target_names = [str(column["name"]) for column in target_columns]
        target_pk = statistics_meta_primary_key(target_columns)
        if not target_pk or "statistic_id" not in target_names:
            raise AppError("Target statistics_meta table has no supported primary key/statistic_id columns.")

        existing = target_conn.execute(
            f"SELECT {quote_identifier(target_pk)} AS metadata_id FROM statistics_meta WHERE statistic_id = ?",
            (self.target_entity,),
        ).fetchone()
        if existing:
            return int(existing["metadata_id"])

        source_keys = set(source_meta_row.keys())
        insert_columns: list[str] = []
        values: list[Any] = []
        for column in target_names:
            if column == target_pk:
                continue
            if column == "statistic_id":
                value = self.target_entity
            elif column in source_keys and column != source_meta_pk:
                value = source_meta_row[column]
            else:
                continue
            insert_columns.append(column)
            values.append(value)

        if "statistic_id" not in insert_columns:
            raise AppError("Target statistics_meta table cannot store the mapped statistic_id.")

        placeholders = ", ".join("?" for _ in values)
        column_sql = ", ".join(quote_identifier(column) for column in insert_columns)
        cursor = target_conn.execute(f"INSERT INTO statistics_meta ({column_sql}) VALUES ({placeholders})", values)
        return int(cursor.lastrowid)

    def existing_target_statistics_meta_id(self, target_conn: sqlite3.Connection) -> int | None:
        if "statistics_meta" not in table_names(target_conn):
            return None
        columns = table_columns(target_conn, "statistics_meta")
        names = [str(column["name"]) for column in columns]
        pk = statistics_meta_primary_key(columns)
        if not pk or "statistic_id" not in names:
            return None
        row = target_conn.execute(
            f"SELECT {quote_identifier(pk)} AS metadata_id FROM statistics_meta WHERE statistic_id = ?",
            (self.target_entity,),
        ).fetchone()
        return int(row["metadata_id"]) if row else None

    def copy_statistics(
        self,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        target_statistics_meta_id: int | None,
    ) -> dict[str, Any]:
        source_tables = table_names(source_conn)
        target_tables = table_names(target_conn)
        if "statistics_meta" not in source_tables:
            return {"enabled": True, "metadata": "source_missing", "tables": {}, "inserted": 0, "skipped": 0, "scanned": 0}
        if "statistics_meta" not in target_tables:
            return {"enabled": True, "metadata": "target_missing", "tables": {}, "inserted": 0, "skipped": 0, "scanned": 0}

        source_meta_row, source_meta_pk, _ = self.source_statistics_meta(source_conn)
        if source_meta_row is None or source_meta_pk is None:
            return {"enabled": True, "metadata": "source_entity_missing", "tables": {}, "inserted": 0, "skipped": 0, "scanned": 0}

        if target_statistics_meta_id is None:
            if self.dry_run:
                target_statistics_meta_id = self.existing_target_statistics_meta_id(target_conn)
            else:
                target_statistics_meta_id = self.ensure_target_statistics_meta(source_meta_row, source_meta_pk, target_conn)

        source_metadata_id = source_meta_row[source_meta_pk]
        table_results: dict[str, dict[str, Any]] = {}
        total_scanned = 0
        total_inserted = 0
        total_skipped = 0
        total_replaced = 0
        read_errors: list[str] = []

        for table in STATISTICS_TABLES:
            table_result = self.copy_statistics_table(
                source_conn,
                target_conn,
                table,
                source_metadata_id,
                target_statistics_meta_id,
            )
            table_results[table] = table_result
            total_scanned += table_result["scanned"]
            total_inserted += table_result["inserted"]
            total_skipped += table_result["skipped"]
            total_replaced += table_result.get("replaced", 0)
            read_errors.extend(table_result.get("read_errors") or [])

        return {
            "enabled": True,
            "metadata": "ok" if target_statistics_meta_id is not None else "target_would_be_created",
            "tables": table_results,
            "scanned": total_scanned,
            "inserted": total_inserted,
            "skipped": total_skipped,
            "replaced": total_replaced,
            "read_errors": read_errors,
            "partial": bool(read_errors),
        }

    def copy_statistics_table(
        self,
        source_conn: sqlite3.Connection,
        target_conn: sqlite3.Connection,
        table: str,
        source_metadata_id: Any,
        target_metadata_id: int | None,
    ) -> dict[str, Any]:
        source_tables = table_names(source_conn)
        target_tables = table_names(target_conn)
        if table not in source_tables or table not in target_tables:
            return {"status": "missing_table", "scanned": 0, "inserted": 0, "skipped": 0, "replaced": 0, "first_imported": None, "last_imported": None}

        source_columns = table_columns(source_conn, table)
        target_columns = table_columns(target_conn, table)
        source_names = [str(column["name"]) for column in source_columns]
        target_names = [str(column["name"]) for column in target_columns]
        target_pk = table_primary_key(target_columns)
        start_column = start_time_column(source_names)
        target_start_column = start_time_column(target_names)
        if (
            "metadata_id" not in source_names
            or "metadata_id" not in target_names
            or not start_column
            or start_column != target_start_column
        ):
            return {"status": "unsupported_schema", "scanned": 0, "inserted": 0, "skipped": 0, "replaced": 0, "first_imported": None, "last_imported": None}

        where_parts = ["metadata_id = ?"]
        query_params: list[Any] = [source_metadata_id]
        start_value = datetime_for_column(self.start, start_column)
        end_value = datetime_for_column(self.end, start_column)
        if start_value is not None:
            where_parts.append(f"{quote_identifier(start_column)} >= ?")
            query_params.append(start_value)
        if end_value is not None:
            where_parts.append(f"{quote_identifier(start_column)} <= ?")
            query_params.append(end_value)
        where_clause = " AND ".join(where_parts)
        total_rows = int(
            source_conn.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table)} WHERE {where_clause}",
                query_params,
            ).fetchone()[0]
        )
        scanned = 0
        inserted = 0
        skipped = 0
        replaced = 0
        first_imported = None
        last_imported = None
        read_errors: list[str] = []

        source_sql = f"""
            SELECT *
            FROM {quote_identifier(table)}
            WHERE {where_clause}
            ORDER BY {quote_identifier(start_column)}
            """

        for source_row in query_rows_best_effort(source_conn, source_sql, query_params, read_errors, table):
            scanned += 1
            if scanned == 1 or scanned % 1000 == 0:
                self.check_cancelled()
            row_start = source_row[start_column]
            if target_metadata_id is not None and self.statistics_duplicate(target_conn, table, target_metadata_id, start_column, row_start):
                if self.duplicate_strategy == "skip":
                    skipped += 1
                    continue
                if self.dry_run:
                    replaced += 1
                else:
                    cursor = target_conn.execute(
                        f"DELETE FROM {quote_identifier(table)} WHERE metadata_id = ? AND {quote_identifier(start_column)} = ?",
                        (target_metadata_id, row_start),
                    )
                    replaced += int(cursor.rowcount or 0)

            timestamp = format_db_time(row_start)
            if first_imported is None:
                first_imported = timestamp
            last_imported = timestamp

            if self.dry_run:
                inserted += 1
                if self.progress_callback and scanned % 1000 == 0:
                    self.progress_callback(75, f"{table} geprueft: {scanned}/{total_rows}")
                continue

            if target_metadata_id is None:
                raise AppError("Target statistics metadata could not be created.")

            insert_columns: list[str] = []
            values: list[Any] = []
            source_keys = set(source_row.keys())
            for column in target_names:
                if column == target_pk:
                    continue
                if column == "metadata_id":
                    value = target_metadata_id
                elif column in source_keys:
                    value = source_row[column]
                else:
                    continue
                insert_columns.append(column)
                values.append(value)

            if not insert_columns:
                raise AppError(f"No compatible columns were found for {table}.")
            placeholders = ", ".join("?" for _ in values)
            column_sql = ", ".join(quote_identifier(column) for column in insert_columns)
            target_conn.execute(f"INSERT INTO {quote_identifier(table)} ({column_sql}) VALUES ({placeholders})", values)
            inserted += 1
            if self.progress_callback and scanned % 1000 == 0:
                self.progress_callback(75, f"{table} verarbeitet: {scanned}/{total_rows}")

        return {
            "status": "ok",
            "scanned": scanned,
            "inserted": inserted,
            "skipped": skipped,
            "replaced": replaced,
            "read_errors": read_errors,
            "partial": bool(read_errors),
            "first_imported": first_imported,
            "last_imported": last_imported,
        }

    def statistics_duplicate(
        self,
        target_conn: sqlite3.Connection,
        table: str,
        target_metadata_id: int,
        start_column: str,
        start_value: Any,
    ) -> bool:
        row = target_conn.execute(
            f"""
            SELECT 1
            FROM {quote_identifier(table)}
            WHERE metadata_id = ? AND {quote_identifier(start_column)} = ?
            LIMIT 1
            """,
            (target_metadata_id, start_value),
        ).fetchone()
        return row is not None


def import_history(
    payload: dict[str, Any],
    job_id: str | None = None,
    progress_callback: Any = None,
    cancel_callback: Any = None,
    write_report: bool = True,
) -> dict[str, Any]:
    source_entity = str(payload.get("source_entity_id", "")).strip()
    target_entity = str(payload.get("target_entity_id", "")).strip()
    dry_run = bool(payload.get("dry_run", True))
    confirm = bool(payload.get("confirm", False))
    include_statistics = bool(payload.get("include_statistics", False))
    duplicate_strategy = str(payload.get("duplicate_strategy", "skip")).strip() or "skip"
    start = payload.get("start") or None
    end = payload.get("end") or None

    if not dry_run and not confirm:
        raise AppError("Write import needs explicit confirmation.")
    if duplicate_strategy not in {"skip", "replace"}:
        raise AppError("Duplicate strategy needs to be either skip or replace.")
    if parse_datetime_value(start) and parse_datetime_value(end):
        if parse_datetime_value(start) > parse_datetime_value(end):
            raise AppError("Start time needs to be before end time.")

    options = read_options()
    importer = HistoryImporter(
        source_db=SOURCE_DB,
        target_db=Path(str(options["database_path"])),
        source_entity=source_entity,
        target_entity=target_entity,
        dry_run=dry_run,
        include_statistics=include_statistics,
        start=start,
        end=end,
        duplicate_strategy=duplicate_strategy,
        progress_callback=progress_callback,
        cancel_callback=cancel_callback,
    )
    result = importer.run()
    if write_report:
        result["report"] = write_import_report(payload, result, job_id)
    return result


def preflight_import(payload: dict[str, Any]) -> dict[str, Any]:
    dry_payload = dict(payload)
    dry_payload["dry_run"] = True
    dry_payload["confirm"] = False
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []

    source_entity = str(dry_payload.get("source_entity_id", "")).strip()
    target_entity = str(dry_payload.get("target_entity_id", "")).strip()
    options = read_options()
    target_db = Path(str(options["database_path"]))

    source_analysis = source_cache_analysis(read_source_meta()) if SOURCE_DB.exists() else {"exists": False, "ok": False}
    if source_analysis is None:
        source_analysis = {"exists": False, "ok": False}
    target_analysis = cached_analyze_database(target_db) if target_db.exists() else {"exists": False, "ok": False}
    source_readable = bool(source_analysis.get("sqlite_header")) and bool(source_analysis.get("readable", source_analysis.get("ok")))
    checks.append({"name": "source_database", "ok": source_readable, "details": source_analysis.get("error")})
    checks.append({"name": "target_database", "ok": bool(target_analysis.get("ok")), "details": target_analysis.get("error")})
    if source_analysis.get("partial") or source_analysis.get("read_errors"):
        warnings.append("Die Quelldatenbank hat Integritaets- oder Lesewarnungen. Lesbare Bereiche werden best-effort verwendet.")

    if source_entity == target_entity:
        warnings.append("Quelle und Ziel haben dieselbe Entity ID. Das ist nur sinnvoll, wenn Daten aus einer alten DB ergaenzt werden.")

    if SOURCE_DB.exists() and is_sqlite_file(SOURCE_DB):
        checks.append({"name": "source_entity", "ok": database_entity_exists(SOURCE_DB, source_entity), "details": source_entity})
    else:
        checks.append({"name": "source_entity", "ok": False, "details": "No cached source database."})

    current_entities = {entity["entity_id"] for entity in list_current_entities().get("entities", [])}
    target_exists = target_entity in current_entities
    checks.append({"name": "target_entity", "ok": target_exists, "details": target_entity})
    if not target_exists:
        warnings.append("Die Ziel-Entitaet existiert aktuell nicht. Der Import kann Metadaten anlegen, Home Assistant kennt die Entitaet aber eventuell erst nach Neustart/Integration.")

    if target_db.exists() and SOURCE_DB.exists() and is_sqlite_file(target_db) and is_sqlite_file(SOURCE_DB):
        with open_db(SOURCE_DB, readonly=True) as source_conn, open_db(target_db, readonly=True) as target_conn:
            source_tables = table_names(source_conn)
            target_tables = table_names(target_conn)
            checks.append({"name": "states_table", "ok": "states" in source_tables and "states" in target_tables})
            if dry_payload.get("include_statistics"):
                checks.append({
                    "name": "statistics_tables",
                    "ok": all(table in source_tables and table in target_tables for table in ("statistics_meta", "statistics")),
                })

    preview_result = import_history(dry_payload, write_report=False)
    ok = all(bool(check.get("ok")) for check in checks)
    return {
        "ok": ok,
        "checks": checks,
        "warnings": warnings,
        "preview": preview_result,
    }


def split_entity_id(entity_id: str) -> tuple[str, str]:
    if "." not in entity_id:
        return "", entity_id
    domain, object_id = entity_id.split(".", 1)
    return domain, object_id


def normalized_entity_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def mapping_suggestions(source_entity_id: str, limit: int = 8) -> dict[str, Any]:
    source_entity_id = source_entity_id.strip()
    if not ENTITY_ID_RE.match(source_entity_id):
        raise AppError("Source entity id is invalid.")

    source_domain, source_object = split_entity_id(source_entity_id)
    source_normalized = normalized_entity_part(source_object)
    suggestions: list[dict[str, Any]] = []
    for entity in list_current_entities().get("entities", []):
        target = str(entity["entity_id"])
        target_domain, target_object = split_entity_id(target)
        target_normalized = normalized_entity_part(target_object)
        score = 0
        reasons: list[str] = []
        if target == source_entity_id:
            score += 100
            reasons.append("exakte Entity ID")
        if target_domain == source_domain:
            score += 30
            reasons.append("gleiche Domain")
        if target_object == source_object:
            score += 45
            reasons.append("gleicher Objektname")
        elif source_normalized and target_normalized:
            if target_normalized == source_normalized:
                score += 40
                reasons.append("normalisierter Objektname gleich")
            elif source_normalized in target_normalized or target_normalized in source_normalized:
                score += 18
                reasons.append("Objektname aehnlich")
        if score <= 0:
            continue
        enriched = dict(entity)
        enriched["score"] = score
        enriched["reasons"] = reasons
        suggestions.append(enriched)

    suggestions.sort(key=lambda item: (-int(item["score"]), item["entity_id"]))
    return {"source_entity_id": source_entity_id, "suggestions": suggestions[: max(1, min(limit, 25))]}


def job_cache_uploaded_file(job_id: str, upload_path: Path, original_name: str) -> dict[str, Any]:
    update_job(job_id, progress=10, message="Upload gespeichert. Archiv/Datenbank wird durchsucht.")
    try:
        raise_if_cancelled(job_id)
        result = handle_uploaded_file(upload_path, original_name, cancel_callback=lambda: raise_if_cancelled(job_id))
    finally:
        try:
            upload_path.unlink()
        except OSError:
            pass
    raise_if_cancelled(job_id)
    update_job(job_id, progress=85, message="Recorder-Datenbank analysiert und Cache aktualisiert.")
    return result


def job_cache_device_backup(job_id: str, file_id: str) -> dict[str, Any]:
    update_job(job_id, progress=10, message=f"Backup wird gelesen: {file_id}")
    raise_if_cancelled(job_id)
    result = handle_device_backup_file(file_id, cancel_callback=lambda: raise_if_cancelled(job_id))
    raise_if_cancelled(job_id)
    update_job(job_id, progress=85, message="Recorder-Datenbank aus Backup extrahiert und analysiert.")
    return result


def job_cache_corrupt_database(job_id: str, file_id: str) -> dict[str, Any]:
    update_job(job_id, progress=10, message=f"Defekte Recorder-DB wird zur Rettung geladen: {file_id}")
    raise_if_cancelled(job_id)
    result = cache_corrupt_database(file_id, cancel_callback=lambda: raise_if_cancelled(job_id))
    meta = result.get("meta") or {}
    if meta.get("cache_storage") in {"hardlink", "symlink"}:
        update_job(job_id, progress=65, message=f"Grosse Recorder-DB wurde per {meta['cache_storage']} in den Cache eingebunden.")
    sidecars = meta.get("recovery_sidecars") or {}
    if sidecars:
        update_job(job_id, progress=70, message=f"{len(sidecars)} passende WAL/SHM-Sidecar-Datei(en) wurden mitgeladen.")
    if meta.get("recovery_warnings"):
        for warning in meta["recovery_warnings"]:
            update_job(job_id, progress=75, message=str(warning))
    update_job(job_id, progress=85, message="Defekte Recorder-DB analysiert und als Quelle zwischengespeichert.")
    return result


def job_refresh_cached_database(job_id: str) -> dict[str, Any]:
    if not SOURCE_DB.exists():
        raise AppError("No cached database is available.")
    update_job(job_id, progress=20, message="Cache-Datenbank wird neu analysiert.")
    raise_if_cancelled(job_id)
    meta = read_source_meta() or {}
    analysis = analyze_database(SOURCE_DB)
    remember_analysis(SOURCE_DB, analysis)
    meta.update({"cached_at": now_iso(), "analysis": analysis, "entities_count": int(analysis.get("entities_count") or 0)})
    write_source_meta(meta)
    update_job(job_id, progress=85, message="Cache-Metadaten aktualisiert.")
    return {"meta": meta, "entities_count": meta["entities_count"], "entities_omitted": True}


def job_import_history(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    update_job(job_id, progress=5, message="Import-Vorabpruefung gestartet.")

    def progress(progress_value: int, message: str) -> None:
        raise_if_cancelled(job_id)
        update_job(job_id, progress=progress_value, message=message)

    raise_if_cancelled(job_id)
    result = import_history(payload, job_id=job_id, progress_callback=progress, cancel_callback=lambda: raise_if_cancelled(job_id))
    raise_if_cancelled(job_id)
    update_job(job_id, progress=90, message="Import-Report geschrieben.")
    return result


def job_restore_current_db(job_id: str, backup_id: str) -> dict[str, Any]:
    update_job(job_id, progress=15, message="Restore-Sicherung wird vorbereitet.")
    result = restore_current_database_from_backup(backup_id)
    update_job(job_id, progress=90, message="Aktuelle Datenbank wurde aus Sicherung wiederhergestellt.")
    return result


def job_snapshot_current_db(job_id: str) -> dict[str, Any]:
    update_job(job_id, progress=15, message="Konsistente Sicherung der aktuellen DB wird erstellt.")
    result = create_current_database_snapshot()
    update_job(job_id, progress=90, message="Snapshot erstellt und analysiert.")
    return result


def job_checkpoint_current_db(job_id: str, mode: str = "PASSIVE") -> dict[str, Any]:
    update_job(job_id, progress=15, message=f"WAL-Checkpoint wird ausgefuehrt: {mode.upper()}.")
    result = checkpoint_current_database(mode)
    update_job(job_id, progress=90, message="WAL-Checkpoint abgeschlossen und DB neu analysiert.")
    return result


def job_purge_entity_history(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    entity_ids = normalize_purge_entity_ids(payload)
    update_job(job_id, progress=10, message=f"History-Purge wird vorbereitet: {len(entity_ids)} Entitaet(en).")
    result = purge_entity_histories(payload, job_id=job_id)
    deleted = result.get("deleted") or {}
    update_job(
        job_id,
        progress=90,
        message=(
            f"History-Purge abgeschlossen: {deleted.get('total_datapoints', 0)} Datenpunkt(e), "
            f"{deleted.get('state_attributes', 0)} Attributzeile(n), "
            f"{deleted.get('states_meta', 0) + deleted.get('statistics_meta', 0)} Metadatenzeile(n)."
        ),
    )
    return result


def job_create_config_backup(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    update_job(job_id, progress=10, message="Home-Assistant-Konfigurationsdateien werden gesammelt.")
    result = create_config_backup(payload, cancel_callback=lambda: raise_if_cancelled(job_id))
    update_job(job_id, progress=90, message=f"Konfig-Backup erstellt: {result['id']}")
    return result


def job_restore_config_backup(job_id: str, backup_id: str) -> dict[str, Any]:
    update_job(job_id, progress=10, message=f"Konfig-Restore wird vorbereitet: {backup_id}")
    raise_if_cancelled(job_id)
    result = restore_config_backup(backup_id, cancel_callback=lambda: raise_if_cancelled(job_id))
    update_job(job_id, progress=90, message=f"{result['restored_count']} Konfig-Datei(en) wiederhergestellt.")
    return result


def create_action_job(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", "")).strip()
    if action == "load_backup":
        file_id = str(payload.get("file_id", "")).strip()
        return start_job("load_backup", "Backup laden", job_cache_device_backup, file_id)
    if action == "load_corrupt_database":
        file_id = str(payload.get("file_id", "")).strip()
        return start_job("load_corrupt_database", "Defekte DB zur Rettung laden", job_cache_corrupt_database, file_id)
    if action == "refresh_cache":
        return start_job("refresh_cache", "Cache aktualisieren", job_refresh_cached_database)
    if action == "import":
        import_payload = payload.get("payload")
        if not isinstance(import_payload, dict):
            raise AppError("Import job needs a payload object.")
        return start_job("import", "History importieren", job_import_history, import_payload)
    if action == "restore_current_db":
        backup_id = str(payload.get("backup_id", "")).strip()
        if not bool(payload.get("confirm", False)):
            raise AppError("Restore needs explicit confirmation.")
        return start_job("restore_current_db", "Aktuelle DB wiederherstellen", job_restore_current_db, backup_id)
    if action == "snapshot_current_db":
        return start_job("snapshot_current_db", "Aktuelle DB-Snapshot erstellen", job_snapshot_current_db)
    if action == "checkpoint_current_db":
        if not bool(payload.get("confirm", False)):
            raise AppError("WAL checkpoint needs explicit confirmation.")
        return start_job("checkpoint_current_db", "WAL-Checkpoint ausfuehren", job_checkpoint_current_db, str(payload.get("mode", "PASSIVE")))
    if action == "purge_entity_history":
        purge_payload = payload.get("payload")
        if not isinstance(purge_payload, dict):
            purge_payload = {"entity_id": str(payload.get("entity_id", "")).strip()}
        if not bool(payload.get("confirm", False)):
            raise AppError("History purge needs explicit confirmation.")
        return start_job("purge_entity_history", "Entity-History loeschen", job_purge_entity_history, purge_payload)
    if action == "config_backup":
        backup_payload = payload.get("payload")
        if not isinstance(backup_payload, dict):
            backup_payload = {}
        return start_job("config_backup", "Konfig-Backup erstellen", job_create_config_backup, backup_payload)
    if action == "restore_config_backup":
        backup_id = str(payload.get("backup_id", "")).strip()
        if not bool(payload.get("confirm", False)):
            raise AppError("Config restore needs explicit confirmation.")
        return start_job("restore_config_backup", "Konfig-Backup wiederherstellen", job_restore_config_backup, backup_id)
    raise AppError("Unknown job action.")


def clear_cache() -> dict[str, Any]:
    cleanup_source_database_files(include_meta=True)
    return cache_status()


def app_status() -> dict[str, Any]:
    options = read_options()
    current_db = Path(str(options["database_path"]))
    config_dir = config_backup_dir(options)
    return {
        "time": now_iso(),
        "options": {
            "log_level": str(options.get("log_level", "info")),
            "database_path": str(current_db),
            "cache_path": str(CACHE_DIR),
            "config_backup_path": str(config_dir),
            "max_upload_mb": int(options.get("max_upload_mb", 131072)),
            "create_current_db_backup": bool(options.get("create_current_db_backup", True)),
        },
        "settings": settings_status(options),
        "cache": cache_status(),
        "config_backup": {
            "config_dir": str(CONFIG_DIR),
            "backup_dir": str(config_dir),
            "storage": storage_info(config_dir),
            "components": [
                {
                    "id": component_id,
                    "label": config["label"],
                    "sensitive": bool(config.get("sensitive", False)),
                    "default": component_id in DEFAULT_CONFIG_BACKUP_COMPONENTS,
                }
                for component_id, config in CONFIG_BACKUP_COMPONENTS.items()
            ],
        },
        "current_database": cached_analyze_database(current_db),
        "active_job": active_job(),
    }


def query_int(query: dict[str, list[str]], name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return default


def paginated_source_entities(query: dict[str, list[str]]) -> dict[str, Any]:
    offset = query_int(query, "offset", 0)
    limit = query_int(query, "limit", 100)
    filter_text = query.get("filter", [""])[0]
    page = paginated_database_entities(SOURCE_DB, offset, limit, filter_text)
    page["cache"] = cache_status()
    return page


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "BackupDbRestore/0.5.11"

    def do_GET(self) -> None:
        try:
            parsed_url = urllib.parse.urlparse(self.path)
            path = parsed_url.path
            query = urllib.parse.parse_qs(parsed_url.query)
            if path in ("/", "/index.html"):
                self.serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self.serve_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            elif path == "/styles.css":
                self.serve_file(WEB_DIR / "styles.css", "text/css; charset=utf-8")
            elif path == "/api/health":
                self.send_json({"ok": True, "time": now_iso()})
            elif path == "/api/status":
                self.send_json(app_status())
            elif path == "/api/settings":
                self.send_json(settings_status())
            elif path == "/api/source/entities":
                self.send_json(paginated_source_entities(query))
            elif path == "/api/current/entities":
                if any(name in query for name in ("offset", "limit", "filter")):
                    self.send_json(paginated_current_entities(query))
                else:
                    self.send_json(list_current_entities())
            elif path == "/api/backups":
                self.send_json(
                    list_device_backups(
                        offset=query_int(query, "offset", 0),
                        limit=query_int(query, "limit", 100),
                        filter_text=query.get("filter", [""])[0],
                    )
                )
            elif path == "/api/corrupt-databases":
                self.send_json(
                    list_corrupt_databases(
                        offset=query_int(query, "offset", 0),
                        limit=query_int(query, "limit", 100),
                        filter_text=query.get("filter", [""])[0],
                    )
                )
            elif path == "/api/current-db-backups":
                self.send_json(list_current_db_backups(offset=query_int(query, "offset", 0), limit=query_int(query, "limit", 100)))
            elif path == "/api/reports":
                self.send_json(list_import_reports(offset=query_int(query, "offset", 0), limit=query_int(query, "limit", 50)))
            elif path.startswith("/api/reports/"):
                self.send_json(read_import_report(path.rsplit("/", 1)[-1]))
            elif path == "/api/config-backups":
                self.send_json(list_config_backups(offset=query_int(query, "offset", 0), limit=query_int(query, "limit", 100)))
            elif path.startswith("/api/config-backups/") and path.endswith("/download"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise AppError("Config backup was not found.", HTTPStatus.NOT_FOUND)
                backup_path = resolve_config_backup(parts[2])
                self.serve_download_file(backup_path, "application/gzip", backup_path.name)
            elif path.startswith("/api/config-backups/") and path.endswith("/preview"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise AppError("Config backup was not found.", HTTPStatus.NOT_FOUND)
                self.send_json(preview_config_backup_restore(parts[2]))
            elif path.startswith("/api/config-backups/"):
                self.send_json(read_config_backup(path.rsplit("/", 1)[-1]))
            elif path.startswith("/api/jobs/"):
                self.send_json(get_job(path.rsplit("/", 1)[-1]))
            elif path == "/api/mapping/suggestions":
                self.send_json(
                    mapping_suggestions(
                        query.get("source_entity_id", [""])[0],
                        limit=query_int(query, "limit", 8),
                    )
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except AppError as err:
            self.send_json(app_error_payload(err), status=err.status)
        except Exception as err:
            self.log_error("Unhandled GET error: %s", err)
            self.send_json({"error": str(err)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        try:
            parsed_url = urllib.parse.urlparse(self.path)
            path = parsed_url.path
            if path == "/api/config-backups/upload":
                self.handle_config_backup_upload()
                return
            if path != "/api/upload":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            query = urllib.parse.parse_qs(parsed_url.query)
            self.handle_upload(async_mode=query.get("async", ["0"])[0] in {"1", "true", "yes"})
        except AppError as err:
            self.send_json(app_error_payload(err), status=err.status)
        except Exception as err:
            self.log_error("Unhandled PUT error: %s", err)
            self.send_json({"error": str(err)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/import":
                ensure_no_conflicting_job("import")
                self.send_json(import_history(self.read_json()))
            elif path == "/api/import/preview":
                ensure_no_conflicting_job("import")
                self.send_json(preflight_import(self.read_json()))
            elif path == "/api/current/purge-preview":
                ensure_no_conflicting_job("purge_entity_history")
                self.send_json(preview_entity_history_purge(self.read_json()))
            elif path == "/api/jobs":
                self.send_json(create_action_job(self.read_json()), status=HTTPStatus.ACCEPTED)
            elif path == "/api/settings":
                self.send_json(update_settings(self.read_json()))
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise AppError("Job was not found.", HTTPStatus.NOT_FOUND)
                self.send_json(request_job_cancel(parts[2]))
            elif path == "/api/backups/load":
                payload = self.read_json()
                if bool(payload.get("async", False)):
                    self.send_json(
                        start_job("load_backup", "Backup laden", job_cache_device_backup, str(payload.get("file_id", ""))),
                        status=HTTPStatus.ACCEPTED,
                    )
                else:
                    ensure_no_conflicting_job("load_backup")
                    result = handle_device_backup_file(str(payload.get("file_id", "")))
                    self.send_json(result)
            elif path == "/api/cache/refresh":
                ensure_no_conflicting_job("refresh_cache")
                if not SOURCE_DB.exists():
                    raise AppError("No cached database is available.")
                meta = read_source_meta() or {}
                analysis = analyze_database(SOURCE_DB)
                remember_analysis(SOURCE_DB, analysis)
                meta.update({"cached_at": now_iso(), "analysis": analysis, "entities_count": int(analysis.get("entities_count") or 0)})
                write_source_meta(meta)
                self.send_json({"meta": meta, "entities_count": meta["entities_count"], "entities_omitted": True})
            elif path == "/api/cache/clear":
                ensure_no_conflicting_job("refresh_cache")
                self.send_json(clear_cache())
            elif path == "/api/restore":
                payload = self.read_json()
                if not bool(payload.get("confirm", False)):
                    raise AppError("Restore needs explicit confirmation.")
                self.send_json(restore_current_database_from_backup(str(payload.get("backup_id", ""))))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except AppError as err:
            self.send_json(app_error_payload(err), status=err.status)
        except Exception as err:
            self.log_error("Unhandled POST error: %s", err)
            self.send_json({"error": str(err)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_upload(self, async_mode: bool = False) -> None:
        ensure_no_conflicting_job("upload")
        options = read_options()
        max_bytes = int(options.get("max_upload_mb", 131072)) * 1024 * 1024
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise AppError("Upload is empty.")
        if content_length > max_bytes:
            raise AppError("Upload is larger than the configured limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        original_name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload")
        original_name = Path(original_name).name or "upload"
        upload_path = UPLOAD_DIR / f"upload_{int(time.time())}_{original_name}"

        remaining = content_length
        with upload_path.open("wb") as output:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                output.write(chunk)
                remaining -= len(chunk)
        if remaining:
            raise AppError("Upload ended before all bytes were received.")

        if async_mode:
            self.send_json(
                start_job("upload", "Upload analysieren", job_cache_uploaded_file, upload_path, original_name),
                status=HTTPStatus.ACCEPTED,
            )
            return

        result = handle_uploaded_file(upload_path, original_name)
        try:
            upload_path.unlink()
        except OSError:
            pass
        self.send_json(result)

    def handle_config_backup_upload(self) -> None:
        options = read_options()
        max_bytes = int(options.get("max_upload_mb", 131072)) * 1024 * 1024
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise AppError("Upload is empty.")
        if content_length > max_bytes:
            raise AppError("Upload is larger than the configured limit.", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        original_name = urllib.parse.unquote(self.headers.get("X-Filename") or "config-backup.tar.gz")
        original_name = config_backup_upload_name(original_name)
        backup_dir = ensure_config_backup_dir()
        temp_path = backup_dir / f".upload-{uuid.uuid4().hex}.tmp"

        remaining = content_length
        try:
            with temp_path.open("wb") as output:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    remaining -= len(chunk)
            if remaining:
                raise AppError("Upload ended before all bytes were received.")
            self.send_json(import_config_backup_upload(temp_path, original_name), status=HTTPStatus.CREATED)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as err:
            raise AppError(f"Invalid JSON payload: {err}") from err
        if not isinstance(payload, dict):
            raise AppError("JSON payload needs to be an object.")
        return payload

    def serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_download_file(self, path: Path, content_type: str, file_name: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = path.read_bytes()
        quoted_name = urllib.parse.quote(file_name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted_name}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    ensure_dirs()
    load_persisted_jobs()
    port = int(os.environ.get("BACKUP_DB_RESTORE_PORT", "8099"))
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    print(f"Backup DB Restore UI listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
