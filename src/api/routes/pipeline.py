"""
Nexus AI — Main pipeline route.

POST /pipeline/query  — full end-to-end pipeline
GET  /pipeline/status/{task_id} — live status
POST /pipeline/approve — HITL approval/rejection
GET  /pipeline/watchlist — list watchlist items
POST /pipeline/schedule — add scheduled automation
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel

from config.settings import get_settings
from src.agents.base import MessageBoard
from src.agents.classifier import QueryClassifier, QueryType
from src.agents.orchestrator import OrchestratorAgent
from src.agents.drafter import DrafterAgent
from src.browser.fleet import BrowserFleet
from src.decision.agent import DecisionAgent
from src.meeting.room import MeetingRoom, MeetingState, MeetingStatus
from src.memory.vector_store import vector_memory
from src.scheduler.jobs import ScheduledTask, WatchlistItem, scheduler
from src.security.audit_logger import AuditEvent, audit_logger
from src.security.auth import AuthUser
from src.security.input_guard import ThreatLevel, input_guard
from src.security.pii_masker import pii_masker
from src.utils.db import create_task, get_task_by_id, update_task_status

log = structlog.get_logger(__name__)
router = APIRouter()
_s = get_settings()


# ── Request / Response models ──────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str
    context: Optional[dict] = None


class ApprovalRequest(BaseModel):
    task_id: str
    decision: str       # "approved" | "rejected" | "edited"
    edited_draft: Optional[str] = None


class ScheduleRequest(BaseModel):
    query: str
    cron_expr: str
    description: str = ""


class WatchlistRequest(BaseModel):
    asset: str
    threshold: float
    direction: str = "above"


class PipelineResponse(BaseModel):
    task_id: str
    status: str
    answer: Optional[str] = None
    confidence: Optional[float] = None
    sources: Optional[list[str]] = None
    requires_approval: bool = False
    draft_preview: Optional[str] = None
    message: str = ""


# ── Pipeline ───────────────────────────────────────────────────────────────────

async def run_pipeline(task_id: str, query: str, user_id: str, context: dict) -> dict:
    """
    Full end-to-end pipeline. Called as a background task.
    Updates task status in DB at each stage.
    """
    await update_task_status(task_id, "running")

    try:
        board = MessageBoard(task_id)
        classifier = QueryClassifier()
        orchestrator = OrchestratorAgent()

        # Step 1 — Classify
        cls_result = await classifier.classify(query)
        context["classification"] = cls_result

        await audit_logger.record(
            AuditEvent.AGENT_STARTED,
            detail=f"Classified as {cls_result.query_type.value} (conf={cls_result.confidence:.0%})",
            task_id=task_id, user_id=user_id, agent_id="classifier",
        )

        # Step 2 — Build plan
        plan = await orchestrator.plan(task_id, query, cls_result)

        # Step 3A — Live data path: browser fleet
        if cls_result.query_type == QueryType.LIVE_DATA:
            fleet = BrowserFleet()
            verified_data = await fleet.run(task_id, query, cls_result)
            context["verified_data"] = str(verified_data.get("answer", ""))
            context["sources"] = verified_data.get("sources", [])
            context["confidence"] = verified_data.get("confidence", 0.0)
            await audit_logger.record(
                AuditEvent.BROWSER_SCRAPED,
                detail=f"Browser fleet: {verified_data.get('sources_valid',0)}/6 sources valid",
                task_id=task_id, agent_id="browser_fleet",
            )

        # Step 3B — Action path: draft + HITL
        if cls_result.query_type == QueryType.ACTION:
            drafter = DrafterAgent()
            draft_msg = await drafter.run(task_id, query, board, context)
            board.post(draft_msg)
            await _trigger_hitl(task_id, user_id, draft_msg.content, context)
            await update_task_status(task_id, "pending_approval")
            await audit_logger.record(
                AuditEvent.HITL_TRIGGERED,
                detail="Draft created, HITL gate triggered",
                task_id=task_id, user_id=user_id,
            )
            return {"status": "pending_approval", "draft": draft_msg.content}

        # Step 4 — Agent meeting room
        meeting_state = MeetingState(
            task_id=task_id, query=query, context=context, board=board
        )
        room = MeetingRoom(memory=vector_memory)
        final_state = await room.run(meeting_state)

        await audit_logger.record(
            AuditEvent.DEBATE_ROUND,
            detail=f"Meeting: {final_state.status.value}, rounds={final_state.current_round}, conv={final_state.convergence_score:.3f}",
            task_id=task_id,
        )

        # Step 5 — Decision Agent
        decision_agent = DecisionAgent()
        verdict = await decision_agent.decide(
            task_id=task_id,
            query=query,
            board=final_state.board,
            verified_data=context.get("verified_data", ""),
            sources=context.get("sources", []),
        )

        # Step 6 — PII mask the final answer
        masked = pii_masker.mask(verdict.answer)

        await audit_logger.record(
            AuditEvent.DECISION_RENDERED,
            detail=f"Decision: conf={verdict.confidence:.0%}, sources={len(verdict.sources_used)}",
            task_id=task_id,
        )

        result = {
            "status": "completed",
            "answer": masked.safe_text,
            "confidence": verdict.confidence,
            "sources": verdict.sources_used,
            "uncertainties": verdict.uncertainties,
            "agents_accepted": verdict.agents_accepted,
            "model": _s.decision_model,
        }
        await update_task_status(task_id, "completed", result)

        # Store answer in memory for future reference
        await vector_memory.add(
            text=f"Q: {query}\nA: {masked.safe_text[:500]}",
            source=f"task:{task_id}",
            metadata={"confidence": verdict.confidence, "query_type": cls_result.query_type.value},
        )

        return result

    except Exception as exc:
        log.error("pipeline_failed", task_id=task_id, error=str(exc))
        await update_task_status(task_id, "failed", {"error": str(exc)[:200]})
        await audit_logger.record(
            AuditEvent.TASK_FAILED,
            detail=f"Pipeline error: {str(exc)[:100]}",
            task_id=task_id, severity="WARNING",
        )
        raise


async def _trigger_hitl(task_id: str, user_id: str, draft: str, context: dict) -> None:
    """Store approval request and send Telegram notification."""
    import json
    expiry = time.time() + _s.hitl_expiry_hours * 3600
    try:
        from src.utils.db import _db_path
        import aiosqlite
        path = await _db_path()
        async with aiosqlite.connect(path) as db:
            await db.execute(
                """INSERT INTO approval_queue (id,task_id,user_id,action_type,draft,status,created_at,expires_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), task_id, user_id, "email", draft, "pending", time.time(), expiry),
            )
            await db.commit()
    except Exception as exc:
        log.warning("hitl_db_failed", error=str(exc))

    # Send Telegram notification
    try:
        from src.hitl.gate import hitl_gate
        await hitl_gate.notify(task_id, draft, context)
    except Exception as exc:
        log.warning("hitl_notify_failed", error=str(exc))


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/query", response_model=PipelineResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_query(
    body: QueryRequest,
    current_user: AuthUser,
    background_tasks: BackgroundTasks,
):
    guard = input_guard.check_query(body.query, user_id=current_user.sub)
    if guard.blocked:
        raise HTTPException(status_code=400, detail="Query blocked by input guard.")

    task_id = str(uuid.uuid4())
    await create_task(task_id, current_user.sub, guard.sanitized or body.query, body.query)
    await audit_logger.record(AuditEvent.TASK_CREATED, detail="Pipeline query submitted", task_id=task_id, user_id=current_user.sub)

    context = body.context or {}
    background_tasks.add_task(run_pipeline, task_id, guard.sanitized or body.query, current_user.sub, context)

    return PipelineResponse(task_id=task_id, status="queued", message="Pipeline started. Poll /pipeline/status/{task_id}")


@router.get("/status/{task_id}", response_model=PipelineResponse)
async def get_status(task_id: str, current_user: AuthUser):
    task = await get_task_by_id(task_id)
    if not task or task["user_id"] != current_user.sub:
        raise HTTPException(status_code=404, detail="Task not found")
    r = task.get("result") or {}
    return PipelineResponse(
        task_id=task_id,
        status=task["status"],
        answer=r.get("answer"),
        confidence=r.get("confidence"),
        sources=r.get("sources"),
        requires_approval=task["status"] == "pending_approval",
        draft_preview=r.get("draft"),
        message=r.get("error", ""),
    )


@router.post("/approve")
async def approve_task(body: ApprovalRequest, current_user: AuthUser):
    import aiosqlite
    from src.utils.db import _db_path
    path = await _db_path()

    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT * FROM approval_queue WHERE task_id=? AND user_id=? AND status='pending'",
            (body.task_id, current_user.sub),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No pending approval found")
        await db.execute(
            "UPDATE approval_queue SET status=?,decided_at=?,decision=? WHERE task_id=?",
            (body.decision, time.time(), body.decision, body.task_id),
        )
        await db.commit()

    event = {"approved": AuditEvent.HITL_APPROVED, "rejected": AuditEvent.HITL_REJECTED, "edited": AuditEvent.HITL_EDITED}.get(body.decision, AuditEvent.HITL_REJECTED)
    await audit_logger.record(event, detail=f"User decision: {body.decision}", task_id=body.task_id, user_id=current_user.sub)

    if body.decision == "approved":
        await update_task_status(body.task_id, "executing")
        # TODO: fire executor

    return {"task_id": body.task_id, "decision": body.decision, "status": "recorded"}


@router.post("/schedule")
async def add_schedule(body: ScheduleRequest, current_user: AuthUser):
    task = ScheduledTask(
        user_id=current_user.sub,
        query=body.query,
        cron_expr=body.cron_expr,
        description=body.description,
    )
    scheduler.add_automation(task)
    return {"task_id": task.id, "message": f"Automation scheduled: {body.cron_expr}"}


@router.post("/watchlist")
async def add_watchlist(body: WatchlistRequest, current_user: AuthUser):
    item = WatchlistItem(
        user_id=current_user.sub,
        asset=body.asset,
        threshold=body.threshold,
        direction=body.direction,
    )
    scheduler.add_watchlist(item)
    return {"item_id": item.id, "message": f"Watching {body.asset} {body.direction} {body.threshold}"}
