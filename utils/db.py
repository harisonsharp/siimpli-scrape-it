"""
SQLite store for run history and scraped results.

Tables:
  runs            — one row per job execution (status, duration, item count, error)
  results         — scraped rows as JSON, tagged with the run that produced them
  rate_hits       — reserved request slots per domain (shared by all jobs/processes)
  domain_backoff  — per-domain "do not request before" timestamps (429/503 backoff)

The database path comes from the ``SIIMPLI_DB`` environment variable and
defaults to ``data/siimpli.db``.  A fresh connection is opened per call so the
module is safe to use from scheduler worker threads.
"""
import json
import logging
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/siimpli.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    duration_s  REAL,
    status      TEXT NOT NULL,          -- running | success | empty | failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    item_count  INTEGER,
    error       TEXT,
    pid         INTEGER                 -- process executing the run (to detect abandoned runs)
);
CREATE INDEX IF NOT EXISTS idx_runs_job ON runs (job_name, id DESC);

CREATE TABLE IF NOT EXISTS results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name   TEXT NOT NULL,
    row_key    TEXT NOT NULL,
    run_id     INTEGER NOT NULL REFERENCES runs (id),
    scraped_at TEXT NOT NULL,
    data       TEXT NOT NULL,
    UNIQUE (job_name, row_key)
);

CREATE TABLE IF NOT EXISTS rate_hits (
    domain TEXT NOT NULL,
    ts     REAL NOT NULL                -- unix time the request slot is scheduled for
);
CREATE INDEX IF NOT EXISTS idx_rate_hits ON rate_hits (domain, ts);

CREATE TABLE IF NOT EXISTS domain_backoff (
    domain TEXT PRIMARY KEY,
    until  REAL NOT NULL
);
"""


def get_db_path() -> Path:
    return Path(os.environ.get("SIIMPLI_DB", DEFAULT_DB_PATH))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    if "pid" not in cols:
        try:
            conn.execute("ALTER TABLE runs ADD COLUMN pid INTEGER")
        except sqlite3.OperationalError:  # another process added it first
            pass


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        with conn:  # commit on success, roll back on error
            yield conn
    finally:
        conn.close()


def start_run(job_name: str) -> int:
    """Record the start of a job execution and return its run id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (job_name, started_at, status, pid) VALUES (?, ?, 'running', ?)",
            (job_name, _now(), os.getpid()),
        )
        return cur.lastrowid


def finish_run(
    run_id: int,
    status: str,
    duration_s: float,
    attempts: int,
    item_count: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Close out a run with its final status."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, duration_s=?, status=?, attempts=?, "
            "item_count=?, error=? WHERE id=?",
            (_now(), duration_s, status, attempts, item_count, error, run_id),
        )


def _row_key(row: Dict[str, Any], key: Optional[List[str]], run_id: int, index: int) -> str:
    if not key:
        return f"run{run_id}:{index}"
    missing = [k for k in key if k not in row]
    if missing:
        raise ValueError(f"Row {index} is missing key field(s) {missing}: {row}")
    return json.dumps([row[k] for k in key], default=str)


def store_results(
    job_name: str,
    run_id: int,
    rows: List[Dict[str, Any]],
    key: Optional[List[str]] = None,
) -> None:
    """Store rows for a run in one transaction.

    With ``key`` set, a row whose key already exists for the job replaces the
    stored row (and takes over its run id); otherwise every row is appended.
    """
    scraped_at = _now()
    payload = [
        (job_name, _row_key(row, key, run_id, i), run_id, scraped_at,
         json.dumps(row, ensure_ascii=False, default=str))
        for i, row in enumerate(rows)
    ]
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO results (job_name, row_key, run_id, scraped_at, data) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (job_name, row_key) DO UPDATE SET "
            "run_id=excluded.run_id, scraped_at=excluded.scraped_at, data=excluded.data",
            payload,
        )


def load_results(job_name: str) -> List[Dict[str, Any]]:
    """Return every stored row for a job, oldest insertion first."""
    with _connect() as conn:
        cur = conn.execute(
            "SELECT data FROM results WHERE job_name=? ORDER BY id", (job_name,)
        )
        return [json.loads(r["data"]) for r in cur.fetchall()]


def recent_runs(job_name: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Return the most recent runs, newest first, optionally for one job."""
    _reap_quietly()
    query = "SELECT * FROM runs"
    args: list = []
    if job_name:
        query += " WHERE job_name=?"
        args.append(job_name)
    query += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(query, args).fetchall()]


# ---------------------------------------------------------------------------
# Abandoned-run cleanup
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Whether a process with this id exists (never signals or kills it)."""
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, we just can't query it
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reap_stale_runs() -> int:
    """Mark ``running`` runs whose process no longer exists as ``failed``.

    A run is left ``running`` if its process is killed mid-run.  Runs recorded
    before pids were tracked (pid NULL) are left alone.  Returns how many were
    marked.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, pid FROM runs WHERE status='running' AND pid IS NOT NULL"
        ).fetchall()
        dead = [r for r in rows if not _pid_alive(r["pid"])]
        for r in dead:
            conn.execute(
                "UPDATE runs SET status='failed', finished_at=?, error=? "
                "WHERE id=? AND status='running'",
                (_now(), f"Abandoned: process {r['pid']} exited before the run finished", r["id"]),
            )
        return len(dead)


def _reap_quietly() -> None:
    try:
        reap_stale_runs()
    except Exception as e:  # reading history must never fail because cleanup did
        logger.debug("Stale-run cleanup skipped: %s", e)


# ---------------------------------------------------------------------------
# Per-domain request scheduling (shared across jobs and processes)
# ---------------------------------------------------------------------------

def plan_slot(
    hits: List[float],
    backoff_until: float,
    now: float,
    rate_limit: int,
    gap: float,
) -> float:
    """Return the earliest unix time a new request may be sent.

    ``hits`` are already-scheduled request times for the domain, ascending.
    The slot is at least ``gap`` after the latest hit, not before
    ``backoff_until``, and keeps every 60-second window to ``rate_limit`` hits.
    """
    t = now
    if hits:
        t = max(t, hits[-1] + gap)
    t = max(t, backoff_until)
    while True:
        recent = [h for h in hits if h > t - 60]
        if len(recent) < max(1, rate_limit):
            return t
        t = max(t, recent[len(recent) - max(1, rate_limit)] + 60)


def reserve_slot(
    domain: str,
    rate_limit: int,
    delay_min: float,
    delay_max: float,
    now: Optional[float] = None,
) -> float:
    """Atomically reserve the next request slot for ``domain``.

    Returns how many seconds the caller must wait before sending the request.
    Concurrent callers (threads or processes) are serialised by the database.
    """
    now = time.time() if now is None else now
    gap = random.uniform(delay_min, max(delay_min, delay_max))
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM rate_hits WHERE domain=? AND ts < ?", (domain, now - 60))
        hits = [r["ts"] for r in conn.execute(
            "SELECT ts FROM rate_hits WHERE domain=? ORDER BY ts", (domain,))]
        row = conn.execute("SELECT until FROM domain_backoff WHERE domain=?", (domain,)).fetchone()
        slot = plan_slot(hits, row["until"] if row else 0.0, now, rate_limit, gap)
        conn.execute("INSERT INTO rate_hits (domain, ts) VALUES (?, ?)", (domain, slot))
    return max(0.0, slot - now)


def set_backoff(domain: str, seconds: float, now: Optional[float] = None) -> float:
    """Block ``domain`` for ``seconds`` from now (never shortens an existing backoff).

    Returns the unix time the backoff lasts until.
    """
    now = time.time() if now is None else now
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO domain_backoff (domain, until) VALUES (?, ?) "
            "ON CONFLICT (domain) DO UPDATE SET until=MAX(until, excluded.until)",
            (domain, now + seconds),
        )
        return conn.execute("SELECT until FROM domain_backoff WHERE domain=?", (domain,)).fetchone()["until"]


# ---------------------------------------------------------------------------
# Read helpers (CLI / MCP)
# ---------------------------------------------------------------------------

def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    """Return one run by id, or None."""
    _reap_quietly()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def query_results(
    job_name: str,
    limit: int = 100,
    offset: int = 0,
    where: Optional[Dict[str, Any]] = None,
) -> Tuple[int, List[Dict[str, Any]]]:
    """Page through a job's stored rows, optionally filtered by field equality.

    ``where`` maps field names to the exact value required (``None`` matches a
    missing/null field).  Returns ``(total_matching, rows)`` in insertion order.
    """
    clauses = ["job_name=?"]
    args: List[Any] = [job_name]
    for field, value in (where or {}).items():
        if '"' in field:
            raise ValueError(f"Invalid field name: {field!r}")
        if value is None:
            clauses.append("json_extract(data, ?) IS NULL")
            args.append(f'$."{field}"')
        else:
            clauses.append("json_extract(data, ?) = ?")
            args.extend([f'$."{field}"', value])
    where_sql = " AND ".join(clauses)
    with _connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM results WHERE {where_sql}", args).fetchone()[0]
        cur = conn.execute(
            f"SELECT data FROM results WHERE {where_sql} ORDER BY id LIMIT ? OFFSET ?",
            args + [max(0, limit), max(0, offset)],
        )
        return total, [json.loads(r["data"]) for r in cur.fetchall()]
