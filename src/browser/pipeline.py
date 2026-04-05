"""
Nexus AI — Browser Pipeline.

Full live-data pipeline:
  1. BrowserFleet    — 6 parallel scrapers
  2. OutputValidator — 5-layer check on each result
  3. CrossVerifier   — weighted consensus + spread detection
  4. Grounding Gate  — structures verified_data for agent injection

This is the entry point called by the orchestrator for all live-data queries.
Returns a VerifiedData object that is injected into the agent meeting room context.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import structlog

from config.settings import get_settings
from src.browser.cross_verifier import CrossVerifier, VerifiedData
from src.browser.fleet import BrowserFleet, FleetResult
from src.browser.validator import OutputValidator
from src.security.audit_logger import AuditEvent, audit_logger

log = structlog.get_logger(__name__)
_settings = get_settings()


@dataclass
class PipelineResult:
    task_id: str
    query: str
    verified_data: VerifiedData
    fleet_result: FleetResult
    elapsed_s: float
    pipeline_summary: str

    @property
    def succeeded(self) -> bool:
        return self.verified_data.consensus_value is not None

    @property
    def context_block(self) -> str:
        """The <verified_data> block injected into agent context."""
        return self.verified_data.to_context_block()

    @property
    def grounding_system_addendum(self) -> str:
        """Addition to every agent system prompt — enforces grounding gate."""
        return VerifiedData.GROUNDING_SYSTEM_ADDENDUM


class BrowserPipeline:
    """
    Wires all browser-layer components together.
    Called once per live-data query by the orchestrator.
    """

    def __init__(
        self,
        llm_client=None,
        baseline_cache: Optional[dict] = None,
    ) -> None:
        from src.agents.llm_client import llm_client as _lc
        self._fleet     = BrowserFleet(llm_client=llm_client or _lc)
        self._validator = OutputValidator(baseline_cache=baseline_cache or {})
        self._verifier  = CrossVerifier()

    async def run(
        self,
        task_id: str,
        query: str,
        category: Optional[str] = None,
    ) -> PipelineResult:
        """
        Execute the full live-data pipeline.
        """
        t0 = time.perf_counter()
        log.info("browser_pipeline_start", task_id=task_id, query=query[:60])

        # ── Stage 1: Scrape 6 sources in parallel ─────────────────────────────
        fleet_result = await self._fleet.run(task_id, query, category)

        await audit_logger.record(
            AuditEvent.BROWSER_SCRAPED,
            detail=(
                f"Fleet: {fleet_result.valid_count}/{len(fleet_result.results)} valid, "
                f"category={fleet_result.category}"
            ),
            task_id=task_id,
            metadata={
                "valid": fleet_result.valid_count,
                "blocked": fleet_result.blocked_count,
                "elapsed_s": fleet_result.elapsed_s,
            },
        )

        # ── Stage 2: 5-layer validation on each result ────────────────────────
        validated = await self._validator.validate_all(
            results=fleet_result.results,
            category=fleet_result.category,
            task_id=task_id,
        )

        valid_validated = [v for v in validated if v.valid]
        log.info(
            "validation_complete",
            task_id=task_id,
            valid=len(valid_validated),
            total=len(validated),
        )

        # ── Stage 3: Weighted consensus + spread detection ────────────────────
        verified_data = self._verifier.verify(
            validated=validated,
            category=fleet_result.category,
            query=query,
            total_sources=len(fleet_result.results),
        )

        # ── Stage 4: Grounding gate confirmation ──────────────────────────────
        # Log what will be injected into agent context
        await audit_logger.record(
            AuditEvent.DECISION_RENDERED,
            detail=(
                f"Grounding gate: consensus={verified_data.consensus_raw} "
                f"conf={verified_data.confidence_level} "
                f"sources={verified_data.sources_valid}/{verified_data.sources_total}"
            ),
            task_id=task_id,
            metadata=verified_data.to_dict(),
        )

        elapsed = round(time.perf_counter() - t0, 2)
        summary = (
            f"Browser pipeline: {verified_data.sources_valid}/{verified_data.sources_total} "
            f"sources valid · consensus {verified_data.consensus_raw} · "
            f"confidence {verified_data.confidence_level} · {elapsed}s"
        )

        log.info(
            "browser_pipeline_complete",
            task_id=task_id,
            consensus=verified_data.consensus_raw,
            confidence=verified_data.confidence_level,
            elapsed_s=elapsed,
        )

        return PipelineResult(
            task_id=task_id,
            query=query,
            verified_data=verified_data,
            fleet_result=fleet_result,
            elapsed_s=elapsed,
            pipeline_summary=summary,
        )
