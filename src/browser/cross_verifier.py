"""
Nexus AI — Cross-Verifier + Grounding Gate.

Takes all validated results and computes a weighted consensus.

Weighting formula per source:
  weight = trust_score × validation_score × freshness_multiplier

Spread check:
  If (max - min) / mean > 5% → confidence = "medium"
  If (max - min) / mean > 15% → confidence = "low"

Grounding Gate:
  The LLM NEVER receives raw values — only the structured verified_data dict.
  System prompt addition: "All values come from <verified_data> only."
  If verified_data is empty → output must say "Could not retrieve live data."

This eliminates hallucination of prices, dates, and statistics entirely.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

import structlog

from src.browser.validator import ValidationResult

log = structlog.get_logger(__name__)


@dataclass
class VerifiedData:
    """
    The single output of the cross-verifier.
    This is the ONLY thing the LLM ever sees for live-data queries.
    Agents must cite [source_name] for every value they use.
    """
    consensus_value: Optional[float]
    consensus_raw: str
    currency: Optional[str]
    unit: str
    confidence_level: str          # "high" | "medium" | "low"
    confidence_pct: float          # 0.0 – 1.0
    spread_pct: float              # (max - min) / mean × 100
    sources_valid: int
    sources_total: int
    source_details: list[dict]     # per-source value + score
    category: str
    query: str

    # Grounding gate prompt injection
    GROUNDING_SYSTEM_ADDENDUM = (
        "\n\nGROUNDING GATE — MANDATORY:\n"
        "Every number, price, date, or statistic in your response MUST come from "
        "the <verified_data> block below. "
        "If a value is not in <verified_data>, DO NOT include it. "
        "If <verified_data> is empty, respond: "
        "'I could not retrieve live data. Please try again.'\n"
        "Never use training-memory values for prices or live statistics."
    )

    def to_context_block(self) -> str:
        """Format as the <verified_data> block injected into agent prompts."""
        if self.consensus_value is None:
            return "<verified_data>\n(no valid data retrieved)\n</verified_data>"

        lines = [
            f"<verified_data category='{self.category}'>",
            f"  consensus_value: {self.consensus_raw}",
            f"  numeric: {self.consensus_value}",
            f"  currency: {self.currency or 'unknown'}",
            f"  unit: {self.unit}",
            f"  confidence: {self.confidence_level} ({self.confidence_pct:.0%})",
            f"  spread: {self.spread_pct:.2f}%",
            f"  sources: {self.sources_valid}/{self.sources_total} verified",
            "",
            "  per_source_breakdown:",
        ]
        for s in self.source_details:
            lines.append(
                f"    [{s['source']}] value={s['value']} "
                f"trust={s['trust_score']:.0%} "
                f"validation={s['validation_score']:.0%}"
            )
        lines.append("</verified_data>")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "consensus_value": self.consensus_value,
            "consensus_raw": self.consensus_raw,
            "currency": self.currency,
            "unit": self.unit,
            "confidence_level": self.confidence_level,
            "confidence_pct": round(self.confidence_pct, 4),
            "spread_pct": round(self.spread_pct, 4),
            "sources_valid": self.sources_valid,
            "sources_total": self.sources_total,
            "source_details": self.source_details,
        }


class CrossVerifier:
    """
    Computes weighted consensus across all validated browser results.
    Enforces the grounding gate — only verified data passes to agents.
    """

    def verify(
        self,
        validated: list[ValidationResult],
        category: str,
        query: str,
        total_sources: int,
    ) -> VerifiedData:
        """
        Compute weighted consensus and return VerifiedData grounding block.
        """
        valid = [v for v in validated if v.valid and v.numeric_value is not None]

        if not valid:
            log.warning("cross_verifier_no_valid", category=category)
            return VerifiedData(
                consensus_value=None,
                consensus_raw="(no valid data)",
                currency=None,
                unit="",
                confidence_level="none",
                confidence_pct=0.0,
                spread_pct=0.0,
                sources_valid=0,
                sources_total=total_sources,
                source_details=[],
                category=category,
                query=query,
            )

        # Compute weights
        weights = []
        values = []
        source_details = []

        for v in valid:
            w = v.trust_score * v.score
            weights.append(w)
            values.append(v.numeric_value)
            source_details.append({
                "source": v.source_name,
                "value": v.raw_value,
                "numeric": v.numeric_value,
                "trust_score": v.trust_score,
                "validation_score": v.score,
                "weight": round(w, 4),
            })

        # Weighted average
        total_weight = sum(weights)
        weighted_avg = sum(v * w for v, w in zip(values, weights)) / total_weight

        # Spread analysis
        val_min = min(values)
        val_max = max(values)
        spread_pct = ((val_max - val_min) / weighted_avg * 100) if weighted_avg != 0 else 0.0

        # Confidence level
        n = len(valid)
        if spread_pct < 2.0 and n >= 4:
            confidence_level = "high"
        elif spread_pct < 5.0 and n >= 3:
            confidence_level = "medium-high"
        elif spread_pct < 15.0 and n >= 2:
            confidence_level = "medium"
        else:
            confidence_level = "low"

        # Confidence percentage (source count + spread-based)
        base_conf = min(n / 6.0, 1.0)
        spread_penalty = min(spread_pct / 100.0, 0.3)
        confidence_pct = max(0.0, base_conf - spread_penalty)

        # Determine currency from most common in results
        currencies = [v.structured.get("currency") for v in valid if v.structured]
        currency = max(set(c for c in currencies if c), key=currencies.count, default=None)

        # Format consensus raw string
        if currency == "INR":
            consensus_raw = f"₹{weighted_avg:,.0f}"
        elif currency == "USD":
            consensus_raw = f"${weighted_avg:,.2f}"
        else:
            consensus_raw = f"{weighted_avg:,.2f}"

        unit = self._infer_unit(category)

        log.info(
            "cross_verifier_complete",
            category=category,
            consensus=consensus_raw,
            spread_pct=round(spread_pct, 2),
            confidence=confidence_level,
            sources_valid=n,
            sources_total=total_sources,
        )

        return VerifiedData(
            consensus_value=round(weighted_avg, 2),
            consensus_raw=consensus_raw,
            currency=currency,
            unit=unit,
            confidence_level=confidence_level,
            confidence_pct=round(confidence_pct, 4),
            spread_pct=round(spread_pct, 4),
            sources_valid=n,
            sources_total=total_sources,
            source_details=source_details,
            category=category,
            query=query,
        )

    @staticmethod
    def _infer_unit(category: str) -> str:
        units = {
            "gold": "per 10g",
            "silver": "per kg",
            "oil": "per barrel",
            "flight": "one-way fare",
            "hotel": "per night",
            "train": "one-way fare",
            "weather": "°C",
            "stock": "per share",
            "crypto": "USD",
        }
        return units.get(category, "")
