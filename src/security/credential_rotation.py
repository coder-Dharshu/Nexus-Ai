"""
Nexus AI — Credential Rotation Scheduler (Improvement #5)
Tracks age of all secrets stored in keychain.
Sends Telegram notification when any key is due for rotation (90-day cycle).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

ROTATION_DAYS = 90
WARN_DAYS     = 80   # warn 10 days before due

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credential_age (
    key_name   TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    rotated_at REAL,
    last_alerted REAL
);
"""


@dataclass
class CredentialStatus:
    key_name: str
    age_days: float
    due_in_days: float
    overdue: bool
    warned: bool


class CredentialRotationTracker:

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False

    async def initialize(self) -> None:
        settings = get_settings()
        self._db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        # Register all known keys
        known_keys = [
            settings.groq_keychain_key,
            settings.hf_keychain_key,
            settings.telegram_keychain_key,
            settings.jwt_keychain_username,
        ]
        async with aiosqlite.connect(self._db_path) as db:
            for key in known_keys:
                await db.execute(
                    "INSERT OR IGNORE INTO credential_age (key_name, created_at) VALUES (?,?)",
                    (key, time.time()),
                )
            await db.commit()
        self._ready = True
        log.info("credential_tracker_initialized", keys=len(known_keys))

    async def record_rotation(self, key_name: str) -> None:
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE credential_age SET rotated_at=?, created_at=? WHERE key_name=?",
                (time.time(), time.time(), key_name),
            )
            await db.commit()
        log.info("credential_rotated", key=key_name)

    async def check_all(self) -> list[CredentialStatus]:
        if not self._ready:
            await self.initialize()
        now = time.time()
        results: list[CredentialStatus] = []
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM credential_age")
            rows = await cur.fetchall()
        for row in rows:
            age_s = now - row["created_at"]
            age_d = age_s / 86400
            due_in = ROTATION_DAYS - age_d
            overdue = age_d >= ROTATION_DAYS
            warned  = age_d >= WARN_DAYS
            results.append(CredentialStatus(
                key_name=row["key_name"],
                age_days=round(age_d, 1),
                due_in_days=round(due_in, 1),
                overdue=overdue, warned=warned,
            ))
        return results

    async def send_rotation_alerts(self) -> list[str]:
        """Called by APScheduler daily. Returns list of alert messages."""
        statuses = await self.check_all()
        alerts: list[str] = []
        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            for s in statuses:
                if not (s.overdue or s.warned):
                    continue
                # Don't re-alert within 24h
                cur = await db.execute(
                    "SELECT last_alerted FROM credential_age WHERE key_name=?", (s.key_name,)
                )
                row = await cur.fetchone()
                last_alerted = row[0] if row and row[0] else 0
                if now - last_alerted < 86400:
                    continue
                msg = (
                    f"🔑 Key rotation {'OVERDUE' if s.overdue else 'due soon'}: "
                    f"`{s.key_name}` — age {s.age_days:.0f} days "
                    f"({'overdue!' if s.overdue else f'due in {s.due_in_days:.0f} days'})"
                )
                alerts.append(msg)
                await db.execute(
                    "UPDATE credential_age SET last_alerted=? WHERE key_name=?",
                    (now, s.key_name),
                )
            await db.commit()
        if alerts:
            log.warning("rotation_alerts_sent", count=len(alerts))
        return alerts


credential_tracker = CredentialRotationTracker()
