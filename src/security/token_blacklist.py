"""
Nexus AI — Token Blacklist (Improvement #1)
JWT revocation via JTI (JWT ID) blacklist stored in SQLite.
On logout or suspicious activity, token JTI added here.
Every authenticated request checks this list BEFORE processing.
"""
from __future__ import annotations

import time
from typing import Optional
import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_blacklist (
    jti        TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT 'logout',
    revoked_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bl_jti     ON token_blacklist(jti);
CREATE INDEX IF NOT EXISTS idx_bl_expires ON token_blacklist(expires_at);
"""


class TokenBlacklist:
    """
    Append-only token blacklist backed by SQLite.
    Entries auto-expire when the token's original expiry passes.
    """

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False

    async def initialize(self) -> None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._ready = True
        log.info("token_blacklist_initialized")

    async def revoke(self, jti: str, user_id: str, expires_at: float, reason: str = "logout") -> None:
        """Add a JTI to the blacklist. Idempotent."""
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO token_blacklist (jti, user_id, reason, revoked_at, expires_at) VALUES (?,?,?,?,?)",
                (jti, user_id, reason, time.time(), expires_at),
            )
            await db.commit()
        log.info("token_revoked", jti=jti[:8]+"...", user_id=user_id, reason=reason)

    async def is_revoked(self, jti: str) -> bool:
        """Return True if this JTI is on the blacklist."""
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM token_blacklist WHERE jti = ? AND expires_at > ?",
                (jti, time.time()),
            )
            row = await cur.fetchone()
        return row is not None

    async def purge_expired(self) -> int:
        """Remove expired entries. Called by scheduler nightly."""
        if not self._ready:
            return 0
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM token_blacklist WHERE expires_at <= ?", (time.time(),)
            )
            await db.commit()
            count = cur.rowcount
        if count:
            log.info("blacklist_purged", removed=count)
        return count


token_blacklist = TokenBlacklist()
