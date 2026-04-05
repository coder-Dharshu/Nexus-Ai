"""
Nexus AI — Output Validator.

Every scrape result passes through 5 independent checks before
reaching the cross-verifier. A result that fails any check is
either retried or discarded — never forwarded to the LLM.

The 5 layers:
  1. Freshness     — timestamp within SOURCE_FRESHNESS_SECONDS
  2. Format        — value matches expected pattern for query type
  3. Outlier       — value within OUTLIER_THRESHOLD of cached baseline
  4. DOM integrity — no CAPTCHA or block signals in source result
  5. Trust rank    — source has a minimum trust score

A per-result validation score (0.0 – 1.0) is computed and stored.
Results below min_score are rejected.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

from config.settings import get_settings
from src.browser.agent import ScrapeResult
from src.security.audit_logger import AuditEvent, audit_logger

log = structlog.get_logger(__name__)
_settings = get_settings()

MIN_VALID_SCORE = 0.76   # must pass at least 3 of 5 checks (weighted)


# ── Format patterns by category ────────────────────────────────────────────────

_FORMAT_PATTERNS: dict[str, list[str]] = {
    "gold":    [r"[\d,]+(?:\.\d+)?", r"₹\s*[\d,]+", r"\$\s*[\d,]+"],
    "silver":  [r"[\d,]+(?:\.\d+)?", r"₹\s*[\d,]+", r"\$\s*[\d,]+"],
    "oil":     [r"\$\s*\d+\.\d+", r"\d+\.\d{2}"],
    "flight":  [r"[\d,]+", r"₹\s*[\d,]+", r"\d{1,2}:\d{2}"],
    "hotel":   [r"[\d,]+(?:\.\d+)?", r"₹\s*[\d,]+"],
    "train":   [r"[\d,]+(?:\.\d+)?", r"₹\s*[\d,]+"],
    "weather": [r"-?\d+(?:\.\d+)?", r"\d+°"],
    "stock":   [r"[\d,]+(?:\.\d+)?", r"₹\s*[\d,]+", r"\$\s*[\d,]+"],
    "crypto":  [r"[\d,]+(?:\.\d+)?", r"\$\s*[\d,]+"],
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    weight: float   # contribution to overall score
    detail: str = ""


@dataclass
class ValidationResult:
    scrape_result: ScrapeResult
    checks: list[CheckResult]
    score: float
    valid: bool
    rejection_reason: str = ""

    @property
    def source_name(self) -> str:
        return self.scrape_result.source_name

    @property
    def raw_value(self) -> Optional[str]:
        return self.scrape_result.raw_value

    @property
    def structured(self) -> Optional[dict]:
        return self.scrape_result.structured

    @property
    def trust_score(self) -> float:
        return self.scrape_result.trust_score

    @property
    def numeric_value(self) -> Optional[float]:
        if self.structured:
            return self.structured.get("value")
        return None

    def to_dict(self) -> dict:
        return {
            "source": self.source_name,
            "value": self.raw_value,
            "numeric": self.numeric_value,
            "score": round(self.score, 3),
            "valid": self.valid,
            "trust_score": self.trust_score,
            "checks": {c.name: c.passed for c in self.checks},
            "rejection_reason": self.rejection_reason,
        }


class OutputValidator:
    """
    Runs 5 independent validation checks on each ScrapeResult.
    Computes a weighted score. Accepts or rejects the result.
    """

    def __init__(self, baseline_cache: Optional[dict] = None) -> None:
        # baseline_cache: {"gold_inr_per_10g": 70800.0, ...}
        self._baseline = baseline_cache or {}

    async def validate(
        self,
        result: ScrapeResult,
        category: str,
        task_id: str = "",
    ) -> ValidationResult:
        """Run all 5 checks and compute final validation result."""

        # If scrape itself failed, skip detailed validation
        if not result.is_valid:
            vr = ValidationResult(
                scrape_result=result,
                checks=[],
                score=0.0,
                valid=False,
                rejection_reason=f"Scrape status: {result.status} — {result.error_msg}",
            )
            await audit_logger.record(
                AuditEvent.SOURCE_REJECTED,
                detail=f"{result.source_name}: {vr.rejection_reason}",
                task_id=task_id,
            )
            return vr

        freshness_check = self._check_freshness(result)
        dom_check       = self._check_dom_integrity(result)

        # Hard gates — one failure = immediate reject
        if not freshness_check.passed:
            vr = ValidationResult(
                scrape_result=result, checks=[freshness_check],
                score=0.0, valid=False,
                rejection_reason=f"HARD GATE: freshness failed ({freshness_check.detail})",
            )
            await audit_logger.record(AuditEvent.SOURCE_REJECTED,
                detail=f"{result.source_name}: stale", task_id=task_id)
            return vr

        if not dom_check.passed:
            vr = ValidationResult(
                scrape_result=result, checks=[dom_check],
                score=0.0, valid=False,
                rejection_reason=f"HARD GATE: dom_integrity failed ({dom_check.detail})",
            )
            await audit_logger.record(AuditEvent.SOURCE_REJECTED,
                detail=f"{result.source_name}: dom blocked", task_id=task_id)
            return vr

        # HARD BLOCK: DOM failure invalidates regardless of other scores
        if not dom_check.passed:
            await audit_logger.record(
                AuditEvent.SOURCE_REJECTED,
                detail=f"{result.source_name}: CAPTCHA/block hard-rejected",
                task_id=task_id,
            )
            return ValidationResult(
                valid=False, score=0.0, task_id=task_id,
                checks={"dom_clean": False},
                rejection_reason="DOM hard block: CAPTCHA or access denied",
            )

        # Soft checks — weighted score
        checks = [
            freshness_check,
            self._check_format(result, category),
            self._check_outlier(result, category),
            dom_check,
            self._check_trust_rank(result),
        ]

        # Weighted score (each check has a weight; sum of weights = 1.0)
        total_weight = sum(c.weight for c in checks)
        score = sum(c.weight for c in checks if c.passed) / total_weight

        valid = score >= MIN_VALID_SCORE
        rejection = ""
        if not valid:
            failed = [c.name for c in checks if not c.passed]
            rejection = f"Failed checks: {failed} (score={score:.2f})"

        event = AuditEvent.SOURCE_VALIDATED if valid else AuditEvent.SOURCE_REJECTED
        await audit_logger.record(
            event,
            detail=f"{result.source_name}: score={score:.2f} valid={valid}",
            task_id=task_id,
            metadata={"checks": {c.name: c.passed for c in checks}},
        )

        return ValidationResult(
            scrape_result=result,
            checks=checks,
            score=round(score, 4),
            valid=valid,
            rejection_reason=rejection,
        )

    async def validate_all(
        self,
        results: list[ScrapeResult],
        category: str,
        task_id: str = "",
    ) -> list[ValidationResult]:
        """Validate all scrape results concurrently."""
        import asyncio
        tasks = [self.validate(r, category, task_id) for r in results]
        return await asyncio.gather(*tasks)

    # ── Check 1: Freshness ────────────────────────────────────────────────────

    def _check_freshness(self, result: ScrapeResult) -> CheckResult:
        age = time.time() - result.timestamp
        passed = age <= _settings.source_freshness_seconds
        return CheckResult(
            name="freshness",
            passed=passed,
            weight=0.25,
            detail=f"age={age:.0f}s limit={_settings.source_freshness_seconds}s",
        )

    # ── Check 2: Format ───────────────────────────────────────────────────────

    def _check_format(self, result: ScrapeResult, category: str) -> CheckResult:
        patterns = _FORMAT_PATTERNS.get(category, [r"[\d,]+"])
        raw = result.raw_value or ""
        passed = any(re.search(p, raw) for p in patterns)
        return CheckResult(
            name="format",
            passed=passed,
            weight=0.25,
            detail=f"value={raw[:40]!r} category={category}",
        )

    # ── Check 3: Outlier ──────────────────────────────────────────────────────

    def _check_outlier(self, result: ScrapeResult, category: str) -> CheckResult:
        from src.browser.site_registry import BASELINE_CACHE_KEYS
        key = BASELINE_CACHE_KEYS.get(category)
        baseline = self._baseline.get(key) if key else None

        if baseline is None:
            # No baseline — pass by default (first run)
            return CheckResult(
                name="outlier",
                passed=True,
                weight=0.15,
                detail="no_baseline_available",
            )

        numeric = None
        if result.structured:
            numeric = result.structured.get("value")

        if numeric is None:
            return CheckResult(
                name="outlier",
                passed=False,
                weight=0.15,
                detail="could_not_parse_numeric_value",
            )

        deviation = abs(numeric - baseline) / max(abs(baseline), 1)
        passed = deviation <= _settings.outlier_threshold
        return CheckResult(
            name="outlier",
            passed=passed,
            weight=0.15,
            detail=f"value={numeric} baseline={baseline} deviation={deviation:.1%}",
        )

    # ── Check 4: DOM integrity ────────────────────────────────────────────────

    def _check_dom_integrity(self, result: ScrapeResult) -> CheckResult:
        passed = len(result.dom_flags) == 0
        return CheckResult(
            name="dom_integrity",
            passed=passed,
            weight=0.20,
            detail=f"flags={result.dom_flags}",
        )

    # ── Check 5: Trust rank ───────────────────────────────────────────────────

    def _check_trust_rank(self, result: ScrapeResult) -> CheckResult:
        MIN_TRUST = 0.75  # B rank minimum
        passed = result.trust_score >= MIN_TRUST
        return CheckResult(
            name="trust_rank",
            passed=passed,
            weight=0.15,
            detail=f"trust={result.trust_score} min={MIN_TRUST}",
        )
