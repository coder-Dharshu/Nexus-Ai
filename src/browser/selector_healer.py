"""
Nexus AI — Dynamic Selector Healer (Improvement #9)
When a browser agent gets an empty result from a CSS selector,
this module sends the DOM to the LLM to find a new selector.
Healed selectors are cached in SQLite to avoid repeated LLM calls.
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS selector_cache (
    cache_key     TEXT PRIMARY KEY,
    domain        TEXT NOT NULL,
    query_type    TEXT NOT NULL,
    selector      TEXT NOT NULL,
    healed        INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 1,
    last_used     REAL NOT NULL,
    created_at    REAL NOT NULL
);
"""

_HEALER_SYSTEM = """You are a CSS selector expert for web scraping.
You receive:
1. The target data type we want to extract (e.g. "gold price", "flight price")
2. A truncated HTML/text DOM of the page

Return ONLY a CSS selector that targets the primary data value.
Rules:
- Return a single CSS selector, nothing else
- Prefer specific selectors (data attributes, unique IDs) over generic ones
- The selector should return a single element containing the primary value
- If no clear selector exists, return: NONE"""


class SelectorHealer:

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

    def _cache_key(self, domain: str, query_type: str) -> str:
        return hashlib.md5(f"{domain}:{query_type}".encode()).hexdigest()

    async def get_cached_selector(self, domain: str, query_type: str) -> Optional[str]:
        if not self._ready:
            await self.initialize()
        key = self._cache_key(domain, query_type)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "SELECT selector FROM selector_cache WHERE cache_key=?", (key,)
            )
            row = await cur.fetchone()
        return row[0] if row else None

    async def heal_and_cache(
        self, domain: str, query_type: str, dom_text: str, llm_client
    ) -> Optional[str]:
        """Send DOM to LLM, get healed selector, cache it."""
        if not self._ready:
            await self.initialize()

        # Truncate DOM to first 3000 chars
        truncated_dom = dom_text[:3000]
        prompt = f"Target data: {query_type}\n\nDOM:\n{truncated_dom}"

        try:
            response = await llm_client.chat(
                model=get_settings().researcher_model,
                messages=[{"role": "user", "content": prompt}],
                system=_HEALER_SYSTEM,
                temperature=0.0,
                max_tokens=100,
            )
            selector = response.content.strip().strip('"\'')
            if not selector or selector == "NONE" or len(selector) > 200:
                log.warning("selector_healer_no_result", domain=domain)
                return None

            # Cache the healed selector
            key = self._cache_key(domain, query_type)
            now = time.time()
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO selector_cache
                       (cache_key, domain, query_type, selector, healed, success_count, last_used, created_at)
                       VALUES (?,?,?,?,1,1,?,?)""",
                    (key, domain, query_type, selector, now, now),
                )
                await db.commit()

            log.info("selector_healed", domain=domain, selector=selector[:50])
            return selector

        except Exception as exc:
            log.warning("selector_heal_failed", domain=domain, error=str(exc))
            return None

    async def record_success(self, domain: str, query_type: str) -> None:
        """Increment success count for a working selector."""
        if not self._ready:
            return
        key = self._cache_key(domain, query_type)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE selector_cache SET success_count=success_count+1, last_used=? WHERE cache_key=?",
                (time.time(), key),
            )
            await db.commit()


selector_healer = SelectorHealer()
