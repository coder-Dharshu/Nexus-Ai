"""
Nexus AI — Query Result Cache
SHA-256 hash of (query + subtype) → cached full pipeline result.
TTL varies by query type. Avoids re-running 6-browser pipeline
for identical queries within the freshness window.
"""
from __future__ import annotations
import hashlib, json, time
from typing import Optional
import aiosqlite, structlog
from config.settings import get_settings
log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_cache (
    cache_key   TEXT PRIMARY KEY,
    query_hash  TEXT NOT NULL,
    subtype     TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qc_expires ON query_cache(expires_at);
"""

TTL_MAP = {
    "commodity": 300, "stock": 180, "weather": 600,
    "flight": 300, "train": 300, "hotel": 600,
    "knowledge": 3600, "explain": 3600, "translate": 86400,
    "calculate": 3600, "compare": 3600, "list": 1800,
    "news": 300, "action": 0, "default": 300,
}

class QueryCache:
    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False

    async def _init(self) -> None:
        if self._ready: return
        s = get_settings()
        self._db_path = str(s.database_url).replace("sqlite+aiosqlite:///","")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA); await db.commit()
        self._ready = True

    def _key(self, query: str, subtype: str) -> str:
        return hashlib.sha256(f"{query.lower().strip()}:{subtype}".encode()).hexdigest()[:24]

    async def get(self, query: str, subtype: str) -> Optional[dict]:
        if not get_settings().query_cache_enabled: return None
        await self._init()
        k = self._key(query, subtype)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT result, expires_at FROM query_cache WHERE cache_key=?", (k,))
            row = await cur.fetchone()
            if not row: return None
            if time.time() > row["expires_at"]:
                await db.execute("DELETE FROM query_cache WHERE cache_key=?", (k,))
                await db.commit(); return None
            await db.execute(
                "UPDATE query_cache SET hit_count=hit_count+1 WHERE cache_key=?", (k,))
            await db.commit()
        log.info("query_cache_hit", subtype=subtype, query=query[:40])
        return json.loads(row["result"])

    async def set(self, query: str, subtype: str, result: dict) -> None:
        if not get_settings().query_cache_enabled: return
        ttl = TTL_MAP.get(subtype, TTL_MAP["default"])
        if ttl == 0: return  # action queries never cached
        await self._init()
        k = self._key(query, subtype)
        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO query_cache (cache_key,query_hash,subtype,result,created_at,expires_at) VALUES (?,?,?,?,?,?)",
                (k, k, subtype, json.dumps(result), now, now+ttl))
            await db.commit()
        log.info("query_cache_set", subtype=subtype, ttl=ttl, query=query[:40])

    async def invalidate(self, subtype: str) -> int:
        """Manually invalidate all cache entries for a subtype."""
        await self._init()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM query_cache WHERE subtype=?", (subtype,))
            await db.commit(); return cur.rowcount

    async def purge_expired(self) -> int:
        await self._init()
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute("DELETE FROM query_cache WHERE expires_at<=?", (time.time(),))
            await db.commit(); count = cur.rowcount
        if count: log.info("query_cache_purged", count=count)
        return count

query_cache = QueryCache()
