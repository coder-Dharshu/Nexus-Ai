"""
Nexus AI — Database utilities (updated with dead-letter queue, retry, concurrent task limit).
"""
from __future__ import annotations
import json, time, uuid
from typing import Any, Optional
import aiosqlite, structlog
from config.settings import get_settings
log = structlog.get_logger(__name__)

_APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_login    REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    query           TEXT NOT NULL,
    original_query  TEXT NOT NULL,
    subtype         TEXT NOT NULL DEFAULT 'unknown',
    status          TEXT NOT NULL DEFAULT 'queued',
    result          TEXT,
    error_message   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 3,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    started_at      REAL,
    completed_at    REAL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    query       TEXT NOT NULL,
    subtype     TEXT NOT NULL,
    error       TEXT NOT NULL,
    attempts    INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS approval_queue (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    draft        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    decided_at   REAL,
    decision     TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_user   ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_retry  ON tasks(retry_count, status);
CREATE INDEX IF NOT EXISTS idx_dlq_task     ON dead_letter_queue(task_id);
CREATE INDEX IF NOT EXISTS idx_approval_task ON approval_queue(task_id);
CREATE INDEX IF NOT EXISTS idx_approval_expires ON approval_queue(expires_at);
"""

async def _db_path() -> str:
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return str(s.database_url).replace("sqlite+aiosqlite:///","")

async def init_databases() -> None:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.executescript(_APP_SCHEMA); await db.commit()
    log.info("app_db_initialized", path=path)
    from src.security.audit_logger import audit_logger
    await audit_logger.initialize()

async def check_db_connection() -> bool:
    try:
        path = await _db_path()
        async with aiosqlite.connect(path) as db:
            await db.execute("SELECT 1")
        return True
    except Exception: return False

# ── Users ─────────────────────────────────────────────────────────────────────
async def get_user_by_username(username: str) -> Optional[dict]:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE username=?", (username,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def create_user(username: str, password_hash: str) -> str:
    uid = str(uuid.uuid4())
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO users (id,username,password_hash,created_at) VALUES (?,?,?,?)",
            (uid, username, password_hash, time.time()))
        await db.commit()
    return uid

async def update_last_login(user_id: str) -> None:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), user_id))
        await db.commit()

# ── Tasks ──────────────────────────────────────────────────────────────────────
async def count_active_tasks(user_id: str) -> int:
    """Count tasks currently running or queued for a user."""
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id=? AND status IN ('queued','running')",
            (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def create_task(task_id: str, user_id: str, query: str,
                      original_query: str = "", subtype: str = "unknown") -> None:
    now = time.time()
    # If original_query not provided, default to the sanitised query
    if not original_query:
        original_query = query
    path = await _db_path()
    s = get_settings()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO tasks (id,user_id,query,original_query,subtype,status,max_retries,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, user_id, query, original_query, subtype, "queued", s.task_max_retries, now, now))
        await db.commit()

async def update_task_status(task_id: str, status: str,
                              result: Optional[dict] = None,
                              error: Optional[str] = None) -> None:
    path = await _db_path()
    now = time.time()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE tasks SET status=?, result=?, error_message=?, updated_at=?, "
            "started_at=CASE WHEN ? AND started_at IS NULL THEN ? ELSE started_at END, "
            "completed_at=CASE WHEN ? THEN ? ELSE completed_at END WHERE id=?",
            (status, json.dumps(result) if result else None, error, now,
             status=="running", now,
             status in ("completed","failed","expired"), now,
             task_id))
        await db.commit()

async def increment_task_retry(task_id: str, error: str) -> int:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "UPDATE tasks SET retry_count=retry_count+1, status='queued', "
            "error_message=?, updated_at=? WHERE id=?",
            (error, time.time(), task_id))
        await db.commit()
        cur = await db.execute("SELECT retry_count FROM tasks WHERE id=?", (task_id,))
        row = await cur.fetchone()
        return row[0] if row else 0

async def move_to_dead_letter(task_id: str, user_id: str, query: str,
                               subtype: str, error: str, attempts: int) -> None:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO dead_letter_queue (id,task_id,user_id,query,subtype,error,attempts,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), task_id, user_id, query, subtype, error, attempts, time.time()))
        await db.execute("UPDATE tasks SET status='failed', updated_at=? WHERE id=?",
                         (time.time(), task_id))
        await db.commit()
    log.error("task_dead_lettered", task_id=task_id, attempts=attempts)

async def get_dead_letter_queue(limit: int = 20) -> list[dict]:
    path = await _db_path()
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM dead_letter_queue ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]

async def get_stale_running_tasks(older_than_s: float = 300) -> list[dict]:
    """Return tasks stuck in 'running' for too long."""
    path = await _db_path()
    cutoff = time.time() - older_than_s
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tasks WHERE status='running' AND started_at<?", (cutoff,))
        rows = await cur.fetchall()
    return [dict(r) for r in rows]

async def get_task_by_id(task_id: str) -> Optional[dict]:
    path = await _db_path()
    import datetime
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = await cur.fetchone()
        if not row: return None
        d = dict(row)
        if d.get("result"): d["result"] = json.loads(d["result"])
        d["created_at"] = datetime.datetime.utcfromtimestamp(d["created_at"]).isoformat()+"Z"
        d["updated_at"] = datetime.datetime.utcfromtimestamp(d["updated_at"]).isoformat()+"Z"
        return d

async def list_tasks_for_user(user_id: str, limit: int = 20) -> list[dict]:
    path = await _db_path()
    import datetime
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit))
        rows = await cur.fetchall()
    results = []
    for row in rows:
        d = dict(row)
        if d.get("result"): d["result"] = json.loads(d["result"])
        d["created_at"] = datetime.datetime.utcfromtimestamp(d["created_at"]).isoformat()+"Z"
        d["updated_at"] = datetime.datetime.utcfromtimestamp(d["updated_at"]).isoformat()+"Z"
        results.append(d)
    return results
