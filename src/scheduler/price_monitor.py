"""
Nexus AI — Price Monitor & Watchlist (Improvement #14)
APScheduler jobs check prices every 15 minutes against user-defined thresholds.
Sends Telegram/WhatsApp alert when threshold crossed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    label        TEXT NOT NULL,
    query        TEXT NOT NULL,
    subtype      TEXT NOT NULL,
    threshold_above REAL,
    threshold_below REAL,
    current_value   REAL,
    last_checked    REAL,
    last_alerted    REAL,
    alert_count     INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wl_user   ON watchlist(user_id);
CREATE INDEX IF NOT EXISTS idx_wl_active ON watchlist(active);
"""


class AlertType(str, Enum):
    ABOVE_THRESHOLD = "above_threshold"
    BELOW_THRESHOLD = "below_threshold"
    SIGNIFICANT_CHANGE = "significant_change"   # >2% in one check


@dataclass
class WatchlistAlert:
    watchlist_id: str
    user_id: str
    label: str
    alert_type: AlertType
    current_value: float
    threshold: Optional[float]
    previous_value: Optional[float]
    message: str


class PriceMonitor:

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False

    async def initialize(self) -> None:
        settings = get_settings()
        self._db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._ready = True
        log.info("price_monitor_initialized")

    async def add_watchlist(
        self,
        user_id: str,
        label: str,
        query: str,
        subtype: str,
        threshold_above: Optional[float] = None,
        threshold_below: Optional[float] = None,
    ) -> str:
        if not self._ready:
            await self.initialize()
        import uuid
        wid = str(uuid.uuid4())
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO watchlist
                   (id, user_id, label, query, subtype, threshold_above,
                    threshold_below, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (wid, user_id, label, query, subtype,
                 threshold_above, threshold_below, time.time()),
            )
            await db.commit()
        log.info("watchlist_added", id=wid, label=label, user=user_id)
        return wid

    async def update_value(
        self, watchlist_id: str, new_value: float
    ) -> Optional[WatchlistAlert]:
        """Update current value and check thresholds. Returns alert if triggered."""
        if not self._ready:
            await self.initialize()
        now = time.time()

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM watchlist WHERE id=?", (watchlist_id,))
            item = await cur.fetchone()
            if not item:
                return None

            prev_value = item["current_value"]
            await db.execute(
                "UPDATE watchlist SET current_value=?, last_checked=? WHERE id=?",
                (new_value, now, watchlist_id),
            )
            await db.commit()

        alert: Optional[WatchlistAlert] = None
        # Don't alert more than once per hour for the same item
        last_alerted = item["last_alerted"] or 0
        if now - last_alerted < 3600:
            return None

        if item["threshold_above"] and new_value >= item["threshold_above"]:
            alert = WatchlistAlert(
                watchlist_id=watchlist_id,
                user_id=item["user_id"],
                label=item["label"],
                alert_type=AlertType.ABOVE_THRESHOLD,
                current_value=new_value,
                threshold=item["threshold_above"],
                previous_value=prev_value,
                message=f"📈 {item['label']} crossed above {item['threshold_above']} → now {new_value}",
            )
        elif item["threshold_below"] and new_value <= item["threshold_below"]:
            alert = WatchlistAlert(
                watchlist_id=watchlist_id,
                user_id=item["user_id"],
                label=item["label"],
                alert_type=AlertType.BELOW_THRESHOLD,
                current_value=new_value,
                threshold=item["threshold_below"],
                previous_value=prev_value,
                message=f"📉 {item['label']} dropped below {item['threshold_below']} → now {new_value}",
            )
        elif prev_value and abs(new_value - prev_value) / prev_value > 0.02:
            pct = ((new_value - prev_value) / prev_value) * 100
            arrow = "📈" if pct > 0 else "📉"
            alert = WatchlistAlert(
                watchlist_id=watchlist_id,
                user_id=item["user_id"],
                label=item["label"],
                alert_type=AlertType.SIGNIFICANT_CHANGE,
                current_value=new_value,
                threshold=None,
                previous_value=prev_value,
                message=f"{arrow} {item['label']} changed {pct:+.1f}% → {new_value}",
            )

        if alert:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "UPDATE watchlist SET last_alerted=?, alert_count=alert_count+1 WHERE id=?",
                    (now, watchlist_id),
                )
                await db.commit()
            log.info("watchlist_alert_triggered", label=item["label"], type=alert.alert_type)

        return alert

    async def get_user_watchlist(self, user_id: str) -> list[dict]:
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM watchlist WHERE user_id=? AND active=1 ORDER BY created_at DESC",
                (user_id,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def remove_watchlist(self, watchlist_id: str, user_id: str) -> bool:
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE watchlist SET active=0 WHERE id=? AND user_id=?",
                (watchlist_id, user_id),
            )
            await db.commit()
        return True


price_monitor = PriceMonitor()
