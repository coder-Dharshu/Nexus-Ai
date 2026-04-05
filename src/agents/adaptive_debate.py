"""
Nexus AI — Adaptive Debate Configuration (Improvement #7)
Round count adapts to query complexity.
Simple price queries → 1 round. Complex research → up to 5 rounds.
Convergence threshold also adjusts per query type.
"""
from __future__ import annotations

from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


@dataclass
class DebateConfig:
    max_rounds: int
    convergence_threshold: float
    parallel_agents: bool
    complexity_label: str


# Complexity profiles
_PROFILES: dict[str, DebateConfig] = {
    # Single number lookup — one source of truth, 1 round is enough
    "price_simple":   DebateConfig(1, 0.88, True,  "simple"),
    # Multi-source comparison (flights, hotels) — 2 rounds for thoroughness
    "price_complex":  DebateConfig(2, 0.90, True,  "moderate"),
    # Knowledge query — full 3 rounds, reasoning needs challenge
    "knowledge":      DebateConfig(3, 0.92, True,  "standard"),
    # Multi-part research (top 10 list, comparison, analysis)
    "research":       DebateConfig(4, 0.93, True,  "deep"),
    # High-stakes (financial advice, medical, legal) — maximum rigor
    "high_stakes":    DebateConfig(5, 0.95, False, "rigorous"),
    # Default fallback
    "default":        DebateConfig(3, 0.92, True,  "standard"),
}

_SUBTYPE_MAP = {
    "commodity":   "price_simple",
    "stock":       "price_simple",
    "weather":     "price_simple",
    "flight":      "price_complex",
    "hotel":       "price_complex",
    "train":       "price_complex",
    "explain":     "knowledge",
    "translate":   "knowledge",
    "calculate":   "knowledge",
    "compare":     "research",
    "list":        "research",
    "news":        "research",
    "history":     "research",
    "general":     "knowledge",
}


def get_debate_config(query_type: str, subtype: str, query: str) -> DebateConfig:
    """
    Determine the right debate configuration for a query.
    Also checks for high-stakes keywords in the query text.
    """
    # High-stakes override: medical, legal, financial decisions
    ql = query.lower()
    high_stakes_kw = ["should i invest", "is it safe to take", "legal advice",
                      "medical advice", "should i buy", "is this illegal",
                      "diagnosis", "prescription", "lawsuit"]
    if any(kw in ql for kw in high_stakes_kw):
        config = _PROFILES["high_stakes"]
        log.info("debate_config", profile="high_stakes", query_snippet=query[:40])
        return config

    profile_key = _SUBTYPE_MAP.get(subtype, "default")
    config = _PROFILES.get(profile_key, _PROFILES["default"])
    log.info(
        "debate_config_selected",
        profile=profile_key,
        max_rounds=config.max_rounds,
        threshold=config.convergence_threshold,
        subtype=subtype,
    )
    return config
