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
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_APP_DIR", "/app"))
WEB_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_WEB_DIR", str(APP_DIR / "web")))
DATA_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_DATA_DIR", "/data"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DB_RESTORE_BACKUP_DIR", "/backup"))
CACHE_DIR = DATA_DIR / "cache"
UPLOAD_DIR = DATA_DIR / "uploads"
TMP_DIR = DATA_DIR / "tmp"
CURRENT_DB_BACKUP_DIR = DATA_DIR / "current-db-backups"
REPORT_DIR = DATA_DIR / "import-reports"
DIAGNOSTIC_DIR = DATA_DIR / "diagnostics"

OPTIONS_PATH = DATA_DIR / "options.json"
SOURCE_DB = CACHE_DIR / "source.db"
SOURCE_ORIGINAL = CACHE_DIR / "source_original"
SOURCE_META = CACHE_DIR / "source_meta.json"
STATISTICS_TABLES = ("statistics_short_term", "statistics")
BACKUP_FILE_EXTENSIONS = (".backup", ".tar", ".tar.gz", ".tgz", ".db", ".sqlite", ".sqlite3")
JOB_LOG_LIMIT = 300
JOB_RETENTION_SECONDS = 6 * 60 * 60

DEFAULT_OPTIONS = {
    "database_path": "/homeassistant_config/home-assistant_v2.db",
    "max_upload_mb": 131072,
    "create_current_db_backup": True,
}

ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
JOB_LOCK = threading.Lock()
JOBS: dict[str, dict[str, Any]] = {}


class AppError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "title": job["title"],
        "status": job["status"],
        "progress": job["progress"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "logs": list(job["logs"]),
        "result": job.get("result"),
        "error": job.get("error"),
    }


def cleanup_jobs() -> None:
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with JOB_LOCK:
        stale = [
            job_id
            for job_id, job in JOBS.items()
            if job["status"] in {"succeeded", "failed"} and job.get("finished_monotonic", time.time()) < cutoff
        ]
        for job_id in stale:
            JOBS.pop(job_id, None)


def get_job(job_id: str) -> dict[str, Any]:
    cleanup_jobs()
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise AppError("Job was not found.", HTTPStatus.NOT_FOUND)
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
        "logs": [],
        "result": None,
        "error": None,
    }
    with JOB_LOCK:
        JOBS[job_id] = job

    thread = threading.Thread(target=run_job, args=(job_id, worker, args), daemon=True)
    thread.start()
    return get_job(job_id)


def run_job(job_id: str, worker: Any, args: tuple[Any, ...]) -> None:
    update_job(job_id, status="running", progress=1, message="Job gestartet.")
    try:
        result = worker(job_id, *args)
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "succeeded"
            job["progress"] = 100
            job["result"] = result
            job["updated_at"] = now_iso()
            job["finished_monotonic"] = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] Job abgeschlossen.")
    except AppError as err:
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["progress"] = 100
            job["error"] = err.message
            job["updated_at"] = now_iso()
            job["finished_monotonic"] = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] Fehler: {err.message}")
    except Exception as err:
        with JOB_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["progress"] = 100
            job["error"] = str(err)
            job["updated_at"] = now_iso()
            job["finished_monotonic"] = time.time()
            timestamp = datetime.now().strftime("%H:%M:%S")
            job["logs"].append(f"[{timestamp}] Fehler: {err}")


def ensure_dirs() -> None:
    for path in (CACHE_DIR, UPLOAD_DIR, TMP_DIR, CURRENT_DB_BACKUP_DIR, REPORT_DIR, DIAGNOSTIC_DIR):
        path.mkdir(parents=True, exist_ok=True)


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


def sqlite_uri(path: Path, readonly: bool = False) -> str:
    quoted = urllib.parse.quote(str(path), safe="/:")
    if readonly:
        return f"file:{quoted}?mode=ro"
    return f"file:{quoted}"


def open_db(path: Path, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(sqlite_uri(path, readonly=True), uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(str(path), timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


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


def database_sidecar_files(path: Path) -> dict[str, dict[str, Any]]:
    return {
        "wal": file_info(Path(f"{path}-wal")),
        "shm": file_info(Path(f"{path}-shm")),
        "journal": file_info(Path(f"{path}-journal")),
    }


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
    if db_path.exists() and is_sqlite_file(db_path):
        for entity in list_entities(db_path, limit=None):
            entity_id = entity["entity_id"]
            if entity_id not in entities:
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


def copy_stream(source: Any, destination: Path) -> None:
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


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


def extract_database(upload_path: Path, target_path: Path, original_name: str) -> dict[str, Any]:
    if is_sqlite_file(upload_path):
        shutil.copy2(upload_path, target_path)
        return {"kind": "sqlite", "selected_member": original_name}

    candidates: list[tuple[int, Path, str]] = []

    def scan_archive(path: Path, trail: str, depth: int, temp_root: Path) -> None:
        if depth > 3:
            return
        if not tarfile.is_tarfile(path):
            return
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                member_name = f"{trail}/{member.name}" if trail else member.name
                score = candidate_score(member.name)
                if score < 100:
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    candidate_path = temp_root / f"candidate_{len(candidates)}.db"
                    copy_stream(extracted, candidate_path)
                    if is_sqlite_file(candidate_path):
                        candidates.append((score, candidate_path, member_name))
                elif looks_like_nested_archive(member.name):
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    nested_path = temp_root / f"nested_{depth}_{len(candidates)}.tar"
                    copy_stream(extracted, nested_path)
                    scan_archive(nested_path, member_name, depth + 1, temp_root)

    with tempfile.TemporaryDirectory(dir=str(TMP_DIR)) as temp_dir:
        scan_archive(upload_path, "", 0, Path(temp_dir))
        if not candidates:
            raise AppError("No Home Assistant SQLite database was found in the uploaded file.")

        candidates.sort(key=lambda item: (item[0], item[2]))
        _, selected_path, selected_member = candidates[0]
        shutil.copy2(selected_path, target_path)

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
) -> dict[str, Any]:
    working_db = TMP_DIR / f"source_{int(time.time())}.db"
    extract_info = extract_database(source_path, working_db, original_name)
    analysis = analyze_database(working_db)
    if not analysis["sqlite_header"]:
        raise AppError("The extracted file is not a SQLite database.")

    cleanup_source_database_files()
    if copy_original:
        shutil.copy2(source_path, SOURCE_ORIGINAL)
    else:
        try:
            SOURCE_ORIGINAL.unlink()
        except FileNotFoundError:
            pass
    shutil.copy2(working_db, SOURCE_DB)
    try:
        working_db.unlink()
    except OSError:
        pass

    entities = list_entities(SOURCE_DB, limit=None)
    meta = {
        "cached_at": now_iso(),
        "source_kind": source_kind,
        "original_name": original_name,
        "original_path": original_path,
        "extract": extract_info,
        "analysis": analyze_database(SOURCE_DB),
        "entities_count": len(entities),
    }
    write_source_meta(meta)

    return {"meta": meta, "entities": entities}


def copy_recovery_sidecars(source_path: Path) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    destinations = {
        "wal": Path(f"{SOURCE_DB}-wal"),
        "shm": Path(f"{SOURCE_DB}-shm"),
        "journal": Path(f"{SOURCE_DB}-journal"),
    }
    for kind, sidecar in matching_corrupt_sidecars(source_path).items():
        destination = destinations[kind]
        shutil.copy2(sidecar, destination)
        copied[kind] = {
            "source": str(sidecar),
            "destination": str(destination),
            "size_bytes": destination.stat().st_size,
        }
    return copied


def remove_source_sidecars() -> None:
    for path in source_database_sidecar_paths():
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def cache_corrupt_database(file_id: str) -> dict[str, Any]:
    source_path = resolve_corrupt_database(file_id)
    cleanup_source_database_files(include_meta=True)
    shutil.copy2(source_path, SOURCE_DB)
    copied_sidecars = copy_recovery_sidecars(source_path)

    analysis = analyze_database(SOURCE_DB)
    warnings: list[str] = []
    used_sidecars = copied_sidecars
    if copied_sidecars and not analysis.get("readable", False):
        warnings.append("Quelle war mit WAL/SHM-Sidecars nicht lesbar. Es wurde automatisch ohne Sidecars erneut versucht.")
        remove_source_sidecars()
        used_sidecars = {}
        analysis = analyze_database(SOURCE_DB)

    if not analysis["sqlite_header"]:
        raise AppError("Selected corrupt database is not a SQLite database.")
    if not analysis.get("readable", analysis.get("ok", False)):
        raise AppError(analysis.get("error") or "Selected corrupt database is not readable enough for rescue.")

    entities = list_entities(SOURCE_DB, limit=None)
    meta = {
        "cached_at": now_iso(),
        "source_kind": "corrupt_database",
        "original_name": source_path.name,
        "original_path": str(source_path),
        "recovery_sidecars": used_sidecars,
        "recovery_warnings": warnings,
        "analysis": analysis,
        "entities_count": len(entities),
    }
    write_source_meta(meta)
    return {"meta": meta, "entities": entities}


def handle_uploaded_file(upload_path: Path, original_name: str) -> dict[str, Any]:
    return cache_source_file(upload_path, original_name, "upload", copy_original=True)


def handle_device_backup_file(file_id: str) -> dict[str, Any]:
    backup_path = resolve_device_backup(file_id)
    return cache_source_file(
        backup_path,
        backup_path.name,
        "device_backup",
        copy_original=False,
        original_path=str(backup_path),
    )


def cache_status() -> dict[str, Any]:
    meta = read_source_meta()
    analysis = analyze_database(SOURCE_DB) if SOURCE_DB.exists() else None
    return {
        "has_cached_database": SOURCE_DB.exists(),
        "source_db": str(SOURCE_DB),
        "source_original": str(SOURCE_ORIGINAL) if SOURCE_ORIGINAL.exists() else None,
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
        "created_at": now_iso(),
        "job_id": job_id,
        "payload": safe_payload,
        "result": result,
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
        reports.append(
            {
                "id": path.name,
                "created_at": report.get("created_at"),
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_entity_id": payload.get("source_entity_id"),
                "target_entity_id": payload.get("target_entity_id"),
                "dry_run": payload.get("dry_run"),
                "states_inserted": result.get("inserted"),
                "states_skipped": result.get("skipped"),
                "states_replaced": result.get("replaced"),
                "statistics_inserted": (result.get("statistics") or {}).get("inserted"),
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
        self.attribute_map: dict[int, int | None] = {}
        self.source_warnings: list[str] = []

    def run(self) -> dict[str, Any]:
        if not self.source_db.exists():
            raise AppError("No cached source database is available. Upload or cache a database first.")
        if not self.target_db.exists():
            raise AppError("Current Home Assistant database was not found.")
        if not ENTITY_ID_RE.match(self.source_entity):
            raise AppError("Source entity id is invalid.")
        if not ENTITY_ID_RE.match(self.target_entity):
            raise AppError("Target entity id is invalid.")

        source_analysis = analyze_database(self.source_db)
        target_analysis = analyze_database(self.target_db)
        if not source_analysis["sqlite_header"] or not source_analysis.get("readable", False):
            raise AppError("Source database is not readable enough for import.")
        if not source_analysis.get("ok", False):
            self.source_warnings = list(source_analysis.get("read_errors") or [])
        if not target_analysis["sqlite_header"] or not target_analysis["ok"]:
            raise AppError("Current database is not healthy enough for import.")

        with open_db(self.source_db, readonly=True) as source_conn:
            source_entities = {item["entity_id"] for item in list_entities(self.source_db, limit=None)}
            if self.source_entity not in source_entities:
                raise AppError("Source entity was not found in the cached database.")

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

            with open_db(self.target_db, readonly=False, timeout=30) as target_conn:
                target_conn.execute("PRAGMA busy_timeout = 30000")
                target_conn.execute("BEGIN IMMEDIATE")
                try:
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

    source_analysis = analyze_database(SOURCE_DB) if SOURCE_DB.exists() else {"exists": False, "ok": False}
    target_analysis = analyze_database(target_db) if target_db.exists() else {"exists": False, "ok": False}
    source_readable = bool(source_analysis.get("sqlite_header")) and bool(source_analysis.get("readable", source_analysis.get("ok")))
    checks.append({"name": "source_database", "ok": source_readable, "details": source_analysis.get("error")})
    checks.append({"name": "target_database", "ok": bool(target_analysis.get("ok")), "details": target_analysis.get("error")})
    if source_analysis.get("partial") or source_analysis.get("read_errors"):
        warnings.append("Die Quelldatenbank hat Integritaets- oder Lesewarnungen. Lesbare Bereiche werden best-effort verwendet.")

    if source_entity == target_entity:
        warnings.append("Quelle und Ziel haben dieselbe Entity ID. Das ist nur sinnvoll, wenn Daten aus einer alten DB ergaenzt werden.")

    if SOURCE_DB.exists() and is_sqlite_file(SOURCE_DB):
        source_entities = {entity["entity_id"] for entity in list_entities(SOURCE_DB, limit=None)}
        checks.append({"name": "source_entity", "ok": source_entity in source_entities, "details": source_entity})
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
        result = handle_uploaded_file(upload_path, original_name)
    finally:
        try:
            upload_path.unlink()
        except OSError:
            pass
    update_job(job_id, progress=85, message="Recorder-Datenbank analysiert und Cache aktualisiert.")
    return result


def job_cache_device_backup(job_id: str, file_id: str) -> dict[str, Any]:
    update_job(job_id, progress=10, message=f"Backup wird gelesen: {file_id}")
    result = handle_device_backup_file(file_id)
    update_job(job_id, progress=85, message="Recorder-Datenbank aus Backup extrahiert und analysiert.")
    return result


def job_cache_corrupt_database(job_id: str, file_id: str) -> dict[str, Any]:
    update_job(job_id, progress=10, message=f"Defekte Recorder-DB wird zur Rettung geladen: {file_id}")
    result = cache_corrupt_database(file_id)
    meta = result.get("meta") or {}
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
    entities = list_entities(SOURCE_DB, limit=None)
    meta = read_source_meta() or {}
    meta.update({"cached_at": now_iso(), "analysis": analyze_database(SOURCE_DB), "entities_count": len(entities)})
    write_source_meta(meta)
    update_job(job_id, progress=85, message="Cache-Metadaten aktualisiert.")
    return {"meta": meta, "entities": entities}


def job_import_history(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    update_job(job_id, progress=5, message="Import-Vorabpruefung gestartet.")

    def progress(progress_value: int, message: str) -> None:
        update_job(job_id, progress=progress_value, message=message)

    result = import_history(payload, job_id=job_id, progress_callback=progress)
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
    raise AppError("Unknown job action.")


def clear_cache() -> dict[str, Any]:
    cleanup_source_database_files(include_meta=True)
    return cache_status()


def app_status() -> dict[str, Any]:
    options = read_options()
    current_db = Path(str(options["database_path"]))
    return {
        "time": now_iso(),
        "options": {
            "database_path": str(current_db),
            "max_upload_mb": int(options.get("max_upload_mb", 131072)),
            "create_current_db_backup": bool(options.get("create_current_db_backup", True)),
        },
        "cache": cache_status(),
        "current_database": analyze_database(current_db),
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
    entities = list_entities(SOURCE_DB, limit=None)
    page = paginate_entities(entities, offset, limit, filter_text)
    page["cache"] = cache_status()
    return page


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "BackupDbRestore/0.5.6"

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
            elif path == "/api/source/entities":
                self.send_json(paginated_source_entities(query))
            elif path == "/api/current/entities":
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
            self.send_json({"error": err.message}, status=err.status)
        except Exception as err:
            self.log_error("Unhandled GET error: %s", err)
            self.send_json({"error": str(err)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        try:
            parsed_url = urllib.parse.urlparse(self.path)
            path = parsed_url.path
            if path != "/api/upload":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            query = urllib.parse.parse_qs(parsed_url.query)
            self.handle_upload(async_mode=query.get("async", ["0"])[0] in {"1", "true", "yes"})
        except AppError as err:
            self.send_json({"error": err.message}, status=err.status)
        except Exception as err:
            self.log_error("Unhandled PUT error: %s", err)
            self.send_json({"error": str(err)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/import":
                self.send_json(import_history(self.read_json()))
            elif path == "/api/import/preview":
                self.send_json(preflight_import(self.read_json()))
            elif path == "/api/jobs":
                self.send_json(create_action_job(self.read_json()), status=HTTPStatus.ACCEPTED)
            elif path == "/api/backups/load":
                payload = self.read_json()
                if bool(payload.get("async", False)):
                    self.send_json(
                        start_job("load_backup", "Backup laden", job_cache_device_backup, str(payload.get("file_id", ""))),
                        status=HTTPStatus.ACCEPTED,
                    )
                else:
                    result = handle_device_backup_file(str(payload.get("file_id", "")))
                    self.send_json(result)
            elif path == "/api/cache/refresh":
                if not SOURCE_DB.exists():
                    raise AppError("No cached database is available.")
                entities = list_entities(SOURCE_DB, limit=None)
                meta = read_source_meta() or {}
                meta.update({"cached_at": now_iso(), "analysis": analyze_database(SOURCE_DB), "entities_count": len(entities)})
                write_source_meta(meta)
                self.send_json({"meta": meta, "entities": entities})
            elif path == "/api/cache/clear":
                self.send_json(clear_cache())
            elif path == "/api/restore":
                payload = self.read_json()
                if not bool(payload.get("confirm", False)):
                    raise AppError("Restore needs explicit confirmation.")
                self.send_json(restore_current_database_from_backup(str(payload.get("backup_id", ""))))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        except AppError as err:
            self.send_json({"error": err.message}, status=err.status)
        except Exception as err:
            self.log_error("Unhandled POST error: %s", err)
            self.send_json({"error": str(err)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_upload(self, async_mode: bool = False) -> None:
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
    server = ThreadingHTTPServer(("0.0.0.0", 8099), RequestHandler)
    print("Backup DB Restore UI listening on :8099", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
