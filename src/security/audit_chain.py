"""
Nexus AI — Tamper-Evident Audit Log (Improvement #4)
Each audit entry has a SHA-256 hash chaining to the previous entry.
This makes the log tamper-evident — any modification breaks the chain.
Verification tool checks the entire chain on demand.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_chain (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    timestamp   REAL NOT NULL,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chain_seq ON audit_chain(seq);
"""

_GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"


def _compute_hash(seq: int, entry_id: str, event: str, detail: str,
                  metadata: str, timestamp: float, prev_hash: str) -> str:
    payload = json.dumps({
        "seq": seq, "entry_id": entry_id, "event": event,
        "detail": detail, "metadata": metadata,
        "timestamp": timestamp, "prev_hash": prev_hash,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditChain:
    """
    Blockchain-style append-only audit log.
    Each entry's hash includes the previous entry's hash.
    Tamper = chain break = detectable.
    """

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False

    async def initialize(self) -> None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = str(settings.audit_database_url).replace("sqlite+aiosqlite:///", "")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._ready = True
        log.info("audit_chain_initialized", path=self._db_path)

    async def append(self, entry_id: str, event: str, detail: str,
                     metadata: Optional[dict] = None) -> str:
        if not self._ready:
            await self.initialize()
        import uuid
        meta_str = json.dumps(metadata or {})
        ts = time.time()

        async with aiosqlite.connect(self._db_path) as db:
            # Get last entry's hash and seq
            cur = await db.execute(
                "SELECT seq, entry_hash FROM audit_chain ORDER BY seq DESC LIMIT 1"
            )
            row = await cur.fetchone()
            prev_seq = row[0] if row else 0
            prev_hash = row[1] if row else _GENESIS_HASH
            new_seq = prev_seq + 1

            entry_hash = _compute_hash(new_seq, entry_id, event, detail, meta_str, ts, prev_hash)
            await db.execute(
                """INSERT INTO audit_chain
                   (entry_id, event, detail, metadata, timestamp, prev_hash, entry_hash)
                   VALUES (?,?,?,?,?,?,?)""",
                (entry_id, event, detail, meta_str, ts, prev_hash, entry_hash),
            )
            await db.commit()
        return entry_hash

    async def verify_chain(self) -> dict:
        """
        Walk every entry and recompute hashes.
        Returns: {valid: bool, total: int, broken_at: seq|None, message: str}
        """
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM audit_chain ORDER BY seq ASC")
            rows = await cur.fetchall()

        if not rows:
            return {"valid": True, "total": 0, "broken_at": None, "message": "Empty chain"}

        prev_hash = _GENESIS_HASH
        for row in rows:
            expected = _compute_hash(
                row["seq"], row["entry_id"], row["event"],
                row["detail"], row["metadata"], row["timestamp"], prev_hash,
            )
            if expected != row["entry_hash"]:
                log.error("audit_chain_broken", seq=row["seq"])
                return {
                    "valid": False, "total": len(rows),
                    "broken_at": row["seq"],
                    "message": f"Chain broken at entry #{row['seq']}",
                }
            prev_hash = row["entry_hash"]

        log.info("audit_chain_verified", total=len(rows))
        return {"valid": True, "total": len(rows), "broken_at": None, "message": "Chain intact"}


audit_chain = AuditChain()
