"""
Nexus AI — Session Memory (Improvement #13)
Session-level conversation memory. Each query can reference the previous one.
"What about Mumbai?" after a gold price query resolves to the gold price context.
"Compare with yesterday" injects yesterday's result as context.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_memory (
    session_id  TEXT NOT NULL,
    turn_num    INTEGER NOT NULL,
    query       TEXT NOT NULL,
    query_type  TEXT NOT NULL,
    subtype     TEXT NOT NULL,
    result      TEXT,
    entities    TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    PRIMARY KEY (session_id, turn_num)
);
CREATE INDEX IF NOT EXISTS idx_sm_session ON session_memory(session_id);
"""

# Patterns that indicate a follow-up query
_FOLLOWUP_PATTERNS = [
    re.compile(r"\bwhat about\b", re.I),
    re.compile(r"\bhow about\b", re.I),
    re.compile(r"\band\s+(?:what|how)\b", re.I),
    re.compile(r"\bcompare\s+(?:with|to)\s+(?:yesterday|last|previous)\b", re.I),
    re.compile(r"\bsame\s+(?:for|in|at|but)\b", re.I),
    re.compile(r"\bwhat\s+(?:was|is)\s+(?:it|that|this)\b", re.I),
    re.compile(r"^\s*(?:and|also|what|how)\s+(?:about|if|when)\b", re.I),
    re.compile(r"^\s*(?:show|give|tell)\s+me\s+(?:the\s+same|more|also)\b", re.I),
]

# Short queries (< 5 words) are likely follow-ups
_SHORT_QUERY_THRESHOLD = 5


@dataclass
class SessionContext:
    session_id: str
    previous_query: Optional[str] = None
    previous_type: Optional[str] = None
    previous_subtype: Optional[str] = None
    previous_result: Optional[dict] = None
    previous_entities: dict = field(default_factory=dict)
    turn_num: int = 0
    is_followup: bool = False
    enriched_query: Optional[str] = None  # query with context injected


class SessionMemory:

    def __init__(self) -> None:
        self._db_path: Optional[str] = None
        self._ready = False
        # In-memory cache: session_id → last turn data
        self._cache: dict[str, dict] = {}

    async def initialize(self) -> None:
        settings = get_settings()
        self._db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        self._ready = True

    def _is_followup(self, query: str) -> bool:
        """Detect if query is referencing a previous turn."""
        word_count = len(query.strip().split())
        if word_count <= _SHORT_QUERY_THRESHOLD:
            return True
        return any(p.search(query) for p in _FOLLOWUP_PATTERNS)

    def _enrich_query(self, query: str, context: SessionContext) -> str:
        """Inject previous context into ambiguous follow-up query."""
        if not context.previous_query:
            return query
        # Extract what's new in the follow-up
        ql = query.lower()
        # If "what about X" → "what is the [previous concept] for X"
        m = re.search(r"what about\s+(.+)", ql, re.I)
        if m and context.previous_subtype:
            new_entity = m.group(1).strip()
            enriched = f"What is the {context.previous_subtype} for {new_entity}?"
            log.info("query_enriched", original=query, enriched=enriched)
            return enriched
        # Generic enrichment: prepend context
        prev_snippet = context.previous_query[:60]
        enriched = f"{query} [context: previously asked about {prev_snippet}]"
        return enriched

    async def process(
        self,
        session_id: str,
        query: str,
        query_type: str,
        subtype: str,
        entities: dict,
    ) -> SessionContext:
        """
        Process a new query in the context of its session.
        Returns enriched context if follow-up detected.
        """
        if not self._ready:
            await self.initialize()

        # Get cached last turn
        last = self._cache.get(session_id)
        ctx = SessionContext(session_id=session_id)

        if last:
            ctx.previous_query   = last.get("query")
            ctx.previous_type    = last.get("query_type")
            ctx.previous_subtype = last.get("subtype")
            ctx.previous_result  = last.get("result")
            ctx.previous_entities= last.get("entities", {})
            ctx.turn_num         = last.get("turn_num", 0) + 1
            ctx.is_followup      = self._is_followup(query)
            if ctx.is_followup:
                ctx.enriched_query = self._enrich_query(query, ctx)

        log.info("session_context",
                 session_id=session_id[:8], turn=ctx.turn_num,
                 is_followup=ctx.is_followup)
        return ctx

    async def save(
        self,
        session_id: str,
        turn_num: int,
        query: str,
        query_type: str,
        subtype: str,
        result: Optional[dict],
        entities: dict,
    ) -> None:
        if not self._ready:
            await self.initialize()
        now = time.time()
        result_str = json.dumps(result) if result else None
        entities_str = json.dumps(entities)

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO session_memory
                   (session_id, turn_num, query, query_type, subtype, result, entities, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (session_id, turn_num, query, query_type, subtype,
                 result_str, entities_str, now),
            )
            await db.commit()

        self._cache[session_id] = {
            "query": query, "query_type": query_type, "subtype": subtype,
            "result": result, "entities": entities, "turn_num": turn_num,
        }

    async def get_history(self, session_id: str, limit: int = 10) -> list[dict]:
        if not self._ready:
            await self.initialize()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT * FROM session_memory WHERE session_id=?
                   ORDER BY turn_num DESC LIMIT ?""",
                (session_id, limit),
            )
            rows = await cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("result"):
                d["result"] = json.loads(d["result"])
            if d.get("entities"):
                d["entities"] = json.loads(d["entities"])
            result.append(d)
        return result


session_memory = SessionMemory()
