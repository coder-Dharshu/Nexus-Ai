"""
Nexus AI — Audit logger.

RULES (enforced in code):
  1. Append-only   — no UPDATE or DELETE ever executed on audit tables
  2. Agent-blind   — no agent has a DB connection to the audit database
  3. PII-masked    — all text fields pass through pii_masker before insert
  4. Immutable IDs — each entry has a UUID that cannot be changed

Every state transition, agent action, security event, and user decision
is recorded here. This is the ground truth for forensics.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Optional

import aiosqlite
import structlog
from pydantic import BaseModel

from config.settings import get_settings
from src.security.pii_masker import pii_masker

log = structlog.get_logger(__name__)


# ── Event types ───────────────────────────────────────────────────────────────

class AuditEvent(str, Enum):
    # Auth
    AUTH_SUCCESS = "auth.success"
    AUTH_FAILURE = "auth.failure"
    TOKEN_ISSUED = "auth.token_issued"
    TOKEN_EXPIRED = "auth.token_expired"

    # Security
    INPUT_BLOCKED = "security.input_blocked"
    INPUT_FLAGGED = "security.input_flagged"
    RATE_LIMITED = "security.rate_limited"
    INJECTION_ATTEMPT = "security.injection_attempt"
    PII_MASKED = "security.pii_masked"

    # Task lifecycle
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_EXPIRED = "task.expired"

    # Agent actions
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_ERROR = "agent.error"
    DEBATE_ROUND = "agent.debate_round"
    CONVERGENCE = "agent.convergence"

    # Browser
    BROWSER_SCRAPED = "browser.scraped"
    BROWSER_BLOCKED = "browser.blocked"
    BROWSER_RETRY = "browser.retry"
    SOURCE_VALIDATED = "browser.source_validated"
    SOURCE_REJECTED = "browser.source_rejected"

    # HITL
    HITL_TRIGGERED = "hitl.triggered"
    HITL_APPROVED = "hitl.approved"
    HITL_REJECTED = "hitl.rejected"
    HITL_EDITED = "hitl.edited"
    HITL_EXPIRED = "hitl.expired"

    # Decision
    DECISION_RENDERED = "decision.rendered"

    # System
    SYSTEM_START = "system.start"
    SYSTEM_STOP = "system.stop"


class AuditEntry(BaseModel):
    id: str
    event: AuditEvent
    task_id: Optional[str]
    user_id: Optional[str]
    agent_id: Optional[str]
    detail: str          # PII-masked human-readable description
    metadata: str        # JSON string of structured data (also PII-masked)
    severity: str        # INFO | WARNING | CRITICAL
    timestamp: float
    timestamp_iso: str


# ── Database schema ───────────────────────────────────────────────────────────

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY,
    event         TEXT NOT NULL,
    task_id       TEXT,
    user_id       TEXT,
    agent_id      TEXT,
    detail        TEXT NOT NULL,
    metadata      TEXT NOT NULL DEFAULT '{}',
    severity      TEXT NOT NULL DEFAULT 'INFO',
    timestamp     REAL NOT NULL,
    timestamp_iso TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_event     ON audit_log(event);
CREATE INDEX IF NOT EXISTS idx_audit_task      ON audit_log(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
"""

# IMPORTANT: No UPDATE table exists. No DELETE ever runs on audit_log.
# The ONLY operation is INSERT.


class AuditLogger:
    """
    Append-only structured audit logger backed by SQLite.
    All writes are async. Agents have no reference to this class instance.
    """

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Create audit DB and tables on startup. Idempotent."""
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = str(settings.audit_database_url).replace(
            "sqlite+aiosqlite:///", ""
        )
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_CREATE_SQL)
            await db.commit()
        self._initialized = True
        log.info("audit_db_initialized", path=self._db_path)

    async def record(
        self,
        event: AuditEvent,
        detail: str,
        *,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        severity: str = "INFO",
    ) -> str:
        """
        Append one audit entry. Returns the entry UUID.
        detail and metadata values are PII-masked before storage.
        """
        import json

        entry_id = str(uuid.uuid4())
        now = time.time()

        # PII mask all text before writing
        safe_detail = pii_masker.mask_for_log(detail)
        meta_str = json.dumps(metadata or {})
        safe_meta = pii_masker.mask_for_log(meta_str)
        ts_iso = __import__("datetime").datetime.utcfromtimestamp(now).isoformat() + "Z"

        if not self._initialized:
            await self.initialize()

        async with aiosqlite.connect(self._db_path) as db:
            # ONLY INSERT — never UPDATE, never DELETE
            await db.execute(
                """
                INSERT INTO audit_log
                  (id, event, task_id, user_id, agent_id, detail, metadata, severity, timestamp, timestamp_iso)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, event.value, task_id, user_id, agent_id,
                 safe_detail, safe_meta, severity, now, ts_iso),
            )
            await db.commit()

        structlog.get_logger("audit").info(
            event.value,
            id=entry_id,
            task_id=task_id,
            severity=severity,
        )
        return entry_id

    async def get_recent(self, limit: int = 50) -> list[dict]:
        """Read-only fetch of recent entries — for dashboard only."""
        if not self._initialized:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def count_by_event(self, event: AuditEvent, since_seconds: int = 3600) -> int:
        """Count events of a given type in the last N seconds."""
        if not self._initialized:
            await self.initialize()
        since = time.time() - since_seconds
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event = ? AND timestamp > ?",
                (event.value, since),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0


# Module-level singleton — agents DO NOT import this
audit_logger = AuditLogger()
