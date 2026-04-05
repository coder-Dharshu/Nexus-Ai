"""
Nexus AI — Browser Fleet.

Launches all 6 browser agents simultaneously via asyncio.gather().
Handles retries: if a source is blocked, swaps in the next
source from the registry fallback list.

Returns results ranked by validity and trust score.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

from config.settings import get_settings
from src.browser.agent import BrowserAgent, ScrapeResult
from src.browser.site_registry import SourceEntry, get_sources, detect_category
from src.security.audit_logger import AuditEvent, audit_logger

log = structlog.get_logger(__name__)
_settings = get_settings()

MAX_AGENTS = 6
RETRY_ON_BLOCK = True


@dataclass
class FleetResult:
    task_id: str
    query: str
    category: str
    results: list[ScrapeResult]
    valid_count: int
    blocked_count: int
    error_count: int
    elapsed_s: float
    sources_attempted: list[str] = field(default_factory=list)

    @property
    def valid_results(self) -> list[ScrapeResult]:
        return [r for r in self.results if r.is_valid]

    @property
    def success_rate(self) -> float:
        total = len(self.results)
        return self.valid_count / total if total > 0 else 0.0


class BrowserFleet:
    """
    Parallel browser agent coordinator.
    Launches MAX_AGENTS scrapers simultaneously, retries blocked sources.
    """

    def __init__(self, llm_client=None) -> None:
        from src.agents.llm_client import llm_client as _lc
        self._llm = llm_client or _lc

    async def run(
        self,
        task_id: str,
        query: str,
        category: Optional[str] = None,
    ) -> FleetResult:
        """
        Launch all browser agents for the query category in parallel.
        Returns FleetResult with all scrape results.
        """
        t0 = time.perf_counter()
        cat = category or detect_category(query)
        sources = get_sources(cat)

        if not sources:
            log.warning("no_sources_for_category", category=cat)
            sources = get_sources("gold")  # safe default

        log.info(
            "fleet_start",
            task_id=task_id,
            category=cat,
            sources=len(sources),
            query=query[:60],
        )

        # Launch all agents simultaneously
        agents = [
            BrowserAgent(
                source=src,
                query=query,
                query_category=cat,
                llm_client=self._llm,
            )
            for src in sources[:MAX_AGENTS]
        ]

        tasks = [asyncio.create_task(agent.scrape()) for agent in agents]
        raw_results: list[ScrapeResult] = await asyncio.gather(*tasks, return_exceptions=False)

        # Log each result
        for result in raw_results:
            event = AuditEvent.BROWSER_SCRAPED if result.is_valid else AuditEvent.BROWSER_BLOCKED
            await audit_logger.record(
                event,
                detail=f"{result.source_name}: {result.status} value={result.raw_value}",
                task_id=task_id,
                metadata={
                    "source": result.source_name,
                    "status": result.status,
                    "value": result.raw_value,
                    "latency_ms": result.latency_ms,
                    "dom_flags": result.dom_flags,
                },
            )

        # Retry blocked sources with fallback sources
        blocked = [r for r in raw_results if r.status in ("blocked", "error")]
        if blocked and RETRY_ON_BLOCK and len(sources) > MAX_AGENTS:
            retry_sources = sources[MAX_AGENTS:]  # fallback sources
            retry_tasks = []
            for i, _ in enumerate(blocked[:len(retry_sources)]):
                fallback = retry_sources[i]
                log.info("fleet_retry", source=fallback.name, task_id=task_id)
                agent = BrowserAgent(
                    source=fallback,
                    query=query,
                    query_category=cat,
                    llm_client=self._llm,
                )
                retry_tasks.append(asyncio.create_task(agent.scrape()))
                await audit_logger.record(
                    AuditEvent.BROWSER_RETRY,
                    detail=f"Retrying with fallback: {fallback.name}",
                    task_id=task_id,
                )

            if retry_tasks:
                retry_results = await asyncio.gather(*retry_tasks, return_exceptions=False)
                # Replace blocked results with retry results
                for i, retry_r in enumerate(retry_results):
                    if i < len(blocked):
                        # Replace blocked with retry result
                        idx = raw_results.index(blocked[i])
                        raw_results[idx] = retry_r

        valid   = [r for r in raw_results if r.is_valid]
        blocked = [r for r in raw_results if r.status == "blocked"]
        errors  = [r for r in raw_results if r.status == "error"]

        elapsed = round(time.perf_counter() - t0, 2)
        log.info(
            "fleet_complete",
            task_id=task_id,
            valid=len(valid),
            blocked=len(blocked),
            errors=len(errors),
            elapsed_s=elapsed,
        )

        return FleetResult(
            task_id=task_id,
            query=query,
            category=cat,
            results=raw_results,
            valid_count=len(valid),
            blocked_count=len(blocked),
            error_count=len(errors),
            elapsed_s=elapsed,
            sources_attempted=[r.source_name for r in raw_results],
        )
