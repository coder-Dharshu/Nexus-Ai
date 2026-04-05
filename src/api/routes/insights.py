"""
Nexus AI — Transparency & Insights API (not present in OpenClaw)
Exposes: agent debate transcripts, source citations, confidence breakdown,
         token usage, audit log viewer, trust scores per source.
These endpoints let users VERIFY the answer, not just trust it.
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from src.security.auth import AuthUser
from src.utils.db import get_task_by_id
import structlog

log = structlog.get_logger(__name__)
router = APIRouter()

@router.get("/{task_id}/transcript")
async def get_debate_transcript(task_id: str, current_user: AuthUser):
    """Full agent debate transcript for a task — every agent, every round."""
    task = await get_task_by_id(task_id)
    if not task or task["user_id"] != current_user.sub:
        raise HTTPException(status_code=404, detail="Task not found")
    result = task.get("result") or {}
    return {
        "task_id": task_id,
        "transcript": result.get("transcript", []),
        "rounds": result.get("rounds_taken", 0),
        "convergence_score": result.get("convergence_score", 0),
        "agent_scores": result.get("agent_scores", {}),
    }

@router.get("/{task_id}/sources")
async def get_source_citations(task_id: str, current_user: AuthUser):
    """All sources scraped, their raw values, trust scores, and consensus computation."""
    task = await get_task_by_id(task_id)
    if not task or task["user_id"] != current_user.sub:
        raise HTTPException(status_code=404, detail="Task not found")
    result = task.get("result") or {}
    return {
        "task_id": task_id,
        "sources": result.get("sources", []),
        "consensus": result.get("consensus", {}),
        "spread_pct": result.get("spread_pct", 0),
        "confidence_pct": result.get("confidence", 0),
        "verified_count": result.get("sources_verified", 0),
    }

@router.get("/{task_id}/confidence")
async def get_confidence_breakdown(task_id: str, current_user: AuthUser):
    """Detailed confidence breakdown: why this score, which agents agreed/disagreed."""
    task = await get_task_by_id(task_id)
    if not task or task["user_id"] != current_user.sub:
        raise HTTPException(status_code=404, detail="Task not found")
    result = task.get("result") or {}
    return {
        "task_id": task_id,
        "overall_confidence": result.get("confidence", 0),
        "source_confidence": result.get("source_confidence", 0),
        "agent_confidence": result.get("agent_confidence", 0),
        "disputes_resolved": result.get("disputes_resolved", 0),
        "disputes_unresolved": result.get("disputes_unresolved", 0),
        "verifier_corrections": result.get("verifier_corrections", 0),
    }

@router.get("/token-usage")
async def get_token_usage(current_user: AuthUser):
    """Daily token usage, model breakdown, budget remaining."""
    from src.agents.llm_client import llm_client
    usage = llm_client._tracker.usage(current_user.sub)
    return {**usage, "user_id": current_user.sub[:8] + "..."}

@router.get("/trust-scores")
async def get_source_trust_scores(current_user: AuthUser):
    """Adaptive trust scores for all scraped sources (EMA-updated per query)."""
    try:
        from src.browser.trust_scorer import source_trust_scorer
        scores = await source_trust_scorer.get_all_scores()
        return {"scores": scores, "count": len(scores)}
    except Exception:
        return {"scores": [], "count": 0}
