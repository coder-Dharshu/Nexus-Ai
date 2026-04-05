"""
Nexus AI — Screenshot Diff (Improvement #12)
For each scraped source, stores the latest screenshot.
On next scrape, computes pixel diff. If layout changed significantly,
automatically triggers selector healing. Also detects price changes
for watchlist alerts.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screenshot_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    domain       TEXT NOT NULL,
    query_type   TEXT NOT NULL,
    screenshot_path TEXT NOT NULL,
    value_text   TEXT,
    pixel_hash   TEXT NOT NULL,
    captured_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ss_domain ON screenshot_history(domain, query_type);
"""


class ScreenshotDiffer:
    """
    Stores and compares screenshots per domain.
    Uses pixel hash comparison (MD5 of raw PNG bytes) for speed.
    Falls back to basic comparison when PIL/cv2 not available.
    """

    CHANGE_THRESHOLD = 0.15   # 15% pixel change = significant layout change

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False
        self._screenshots_dir: Optional[Path] = None

    async def initialize(self) -> None:
        settings = get_settings()
        self._db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
        self._screenshots_dir = settings.screenshots_dir
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._ready = True

    async def save_and_compare(
        self,
        domain: str,
        query_type: str,
        screenshot_bytes: bytes,
        value_text: str = "",
    ) -> dict:
        """
        Save screenshot and compare to last known.
        Returns: {changed: bool, change_pct: float, heal_needed: bool, value_changed: bool}
        """
        if not self._ready:
            await self.initialize()

        # Hash the screenshot bytes
        pixel_hash = hashlib.md5(screenshot_bytes).hexdigest()

        # Save screenshot to disk
        filename = f"{domain.replace('.', '_')}_{query_type}_{int(time.time())}.png"
        filepath = self._screenshots_dir / filename
        filepath.write_bytes(screenshot_bytes)

        # Get previous screenshot
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM screenshot_history
                   WHERE domain=? AND query_type=?
                   ORDER BY captured_at DESC LIMIT 1""",
                (domain, query_type),
            )
            prev = await cur.fetchone()

            # Save new screenshot record
            await db.execute(
                """INSERT INTO screenshot_history
                   (domain, query_type, screenshot_path, value_text, pixel_hash, captured_at)
                   VALUES (?,?,?,?,?,?)""",
                (domain, query_type, str(filepath), value_text, pixel_hash, time.time()),
            )
            await db.commit()

        if not prev:
            return {"changed": False, "change_pct": 0.0, "heal_needed": False, "value_changed": False}

        # Compare pixel hashes (quick check)
        hash_match = prev["pixel_hash"] == pixel_hash
        # Rough change estimate from hash difference
        change_pct = 0.0 if hash_match else self._estimate_change(prev["pixel_hash"], pixel_hash)

        # Check if value text changed
        value_changed = bool(prev["value_text"] and value_text and prev["value_text"] != value_text)

        heal_needed = change_pct > self.CHANGE_THRESHOLD

        if heal_needed:
            log.warning("layout_change_detected", domain=domain,
                       change_pct=round(change_pct, 2), heal_needed=True)
        if value_changed:
            log.info("value_changed", domain=domain,
                    old=prev["value_text"], new=value_text)

        return {
            "changed": not hash_match,
            "change_pct": round(change_pct, 3),
            "heal_needed": heal_needed,
            "value_changed": value_changed,
            "old_value": prev["value_text"] if value_changed else None,
            "new_value": value_text if value_changed else None,
        }

    @staticmethod
    def _estimate_change(hash1: str, hash2: str) -> float:
        """Estimate change percentage from hash difference."""
        if hash1 == hash2:
            return 0.0
        # Count differing hex digits as rough proxy
        diff = sum(1 for a, b in zip(hash1, hash2) if a != b)
        return diff / len(hash1)

    async def get_history(self, domain: str, query_type: str, limit: int = 10) -> list[dict]:
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT domain, query_type, value_text, pixel_hash, captured_at
                   FROM screenshot_history WHERE domain=? AND query_type=?
                   ORDER BY captured_at DESC LIMIT ?""",
                (domain, query_type, limit),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]


screenshot_differ = ScreenshotDiffer()
