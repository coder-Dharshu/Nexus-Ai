"""
Nexus AI — Source Trust Score Learning (Improvement #10)
Every source is tracked over time using an exponential moving average.
Sources that consistently agree with consensus get promoted.
Sources that consistently produce outliers get demoted.
"""
from __future__ import annotations

import time
from typing import Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_trust (
    domain          TEXT PRIMARY KEY,
    trust_score     REAL NOT NULL DEFAULT 0.85,
    total_queries   INTEGER NOT NULL DEFAULT 0,
    outlier_count   INTEGER NOT NULL DEFAULT 0,
    agreement_count INTEGER NOT NULL DEFAULT 0,
    last_updated    REAL NOT NULL,
    category        TEXT NOT NULL DEFAULT 'general'
);
"""

# EMA alpha — higher = more reactive to recent data
EMA_ALPHA = 0.15
# Outlier threshold — deviation from consensus
OUTLIER_THRESHOLD = 0.15


class SourceTrustScorer:
    """
    Adaptive trust scorer. After every cross-verification run,
    update each source's trust score based on how it compared to consensus.
    """

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False
        # In-memory cache to avoid DB hits on every request
        self._cache: dict[str, float] = {}

    async def initialize(self) -> None:
        settings = get_settings()
        self._db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        await self._load_cache()
        self._ready = True
        log.info("trust_scorer_initialized")

    async def _load_cache(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT domain, trust_score FROM source_trust")
            rows = await cur.fetchall()
        self._cache = {row["domain"]: row["trust_score"] for row in rows}

    async def get_score(self, domain: str) -> float:
        """Return trust score for domain (0.0–1.0). Default 0.85 for new sources."""
        if not self._ready:
            await self.initialize()
        return self._cache.get(domain, 0.85)

    async def update(
        self,
        domain: str,
        source_value: float,
        consensus_value: float,
        category: str = "general",
    ) -> float:
        """
        Update trust score after a cross-verification run.
        Returns the new trust score.
        """
        if not self._ready:
            await self.initialize()
        if consensus_value == 0:
            return self._cache.get(domain, 0.85)

        deviation = abs(source_value - consensus_value) / abs(consensus_value)
        is_outlier = deviation > OUTLIER_THRESHOLD
        # Agreement score: 1.0 if perfect match, approaches 0 as deviation grows
        agreement = max(0.0, 1.0 - (deviation / OUTLIER_THRESHOLD))

        current_score = self._cache.get(domain, 0.85)
        # EMA update
        new_score = (1 - EMA_ALPHA) * current_score + EMA_ALPHA * agreement
        new_score = max(0.40, min(1.0, new_score))  # clamp 0.40–1.00

        self._cache[domain] = new_score

        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO source_trust
                   (domain, trust_score, total_queries, outlier_count, agreement_count, last_updated, category)
                   VALUES (?,?,1,?,?,?,?)
                   ON CONFLICT(domain) DO UPDATE SET
                     trust_score=excluded.trust_score,
                     total_queries=total_queries+1,
                     outlier_count=outlier_count+excluded.outlier_count,
                     agreement_count=agreement_count+excluded.agreement_count,
                     last_updated=excluded.last_updated""",
                (domain, new_score, 1 if is_outlier else 0,
                 0 if is_outlier else 1, now, category),
            )
            await db.commit()

        if is_outlier:
            log.warning("source_trust_decreased", domain=domain,
                       old=round(current_score, 3), new=round(new_score, 3),
                       deviation=round(deviation, 3))
        else:
            log.debug("source_trust_updated", domain=domain,
                     old=round(current_score, 3), new=round(new_score, 3))
        return new_score

    async def get_all_scores(self) -> list[dict]:
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM source_trust ORDER BY trust_score DESC"
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]


source_trust_scorer = SourceTrustScorer()
