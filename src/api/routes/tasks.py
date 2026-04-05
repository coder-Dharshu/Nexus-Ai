"""
Nexus AI — Task routes (fully wired to pipeline).
POST /tasks/query   → validates → creates task → fires pipeline in BackgroundTask
GET  /tasks/{id}/stream → SSE live progress stream
GET  /tasks/{id}    → poll for status
GET  /tasks/        → list user tasks
"""
from __future__ import annotations
import asyncio, json, time, uuid
from typing import Optional, AsyncGenerator
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import structlog
from config.settings import get_settings
from src.security.audit_logger import AuditEvent, audit_logger
from src.security.auth import AuthUser
from src.security.input_guard import ThreatLevel, input_guard
from src.security.rate_limiter import per_user_limiter
from src.utils.db import (
    create_task, get_task_by_id, list_tasks_for_user,
    count_active_tasks, get_dead_letter_queue,
)

log = structlog.get_logger(__name__)
router = APIRouter()
_s = get_settings()

# SSE event store: task_id → list of events (in-memory, bounded)
_sse_events: dict[str, list[dict]] = {}
_MAX_SSE_EVENTS = 200

class QueryRequest(BaseModel):
    query: str
    session_id: Optional[str] = None
    context: Optional[dict] = None

class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str
    stream_url: str

class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    query: str
    subtype: str
    result: Optional[dict]
    retry_count: int
    created_at: str
    updated_at: str

def _push_event(task_id: str, stage: str, msg: str, **data) -> None:
    """Push a pipeline event to the SSE store for this task."""
    events = _sse_events.setdefault(task_id, [])
    events.append({"stage": stage, "msg": msg, "ts": time.time(), **data})
    if len(events) > _MAX_SSE_EVENTS:
        events.pop(0)

@router.post("/query", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_query(
    body: QueryRequest,
    current_user: AuthUser,
    request: Request,
    background_tasks: BackgroundTasks,
):
    # ── Body size check ──────────────────────────────────────────────────────
    content_length = request.headers.get("content-length", "0")
    if int(content_length) > _s.max_request_body_kb * 1024:
        raise HTTPException(status_code=413, detail="Request body too large")

    # ── Rate limit ────────────────────────────────────────────────────────────
    rl = await per_user_limiter.check(current_user.sub, body.query)
    if not rl.allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: {rl.reason}. Retry after {rl.retry_after:.0f}s",
            headers={"Retry-After": str(int(rl.retry_after or 60))},
        )

    # ── Concurrent task limit ─────────────────────────────────────────────────
    active = await count_active_tasks(current_user.sub)
    if active >= _s.max_concurrent_tasks_per_user:
        raise HTTPException(
            status_code=429,
            detail=f"Too many active tasks ({active}/{_s.max_concurrent_tasks_per_user}). Wait for one to finish.",
        )

    # ── Input guard ───────────────────────────────────────────────────────────
    guard_result = input_guard.check_query(body.query, user_id=current_user.sub)
    if guard_result.blocked:
        await audit_logger.record(
            AuditEvent.INPUT_BLOCKED,
            detail=f"Query blocked score={guard_result.score} flags={guard_result.flags}",
            user_id=current_user.sub, severity="WARNING",
        )
        raise HTTPException(status_code=400, detail="Query blocked by security layer.")
    if guard_result.level == ThreatLevel.INJECTION:
        await audit_logger.record(
            AuditEvent.INPUT_FLAGGED,
            detail=f"Query flagged score={guard_result.score}",
            user_id=current_user.sub, severity="WARNING",
        )

    # ── Create task ───────────────────────────────────────────────────────────
    task_id = str(uuid.uuid4())
    clean_query = guard_result.sanitized or body.query
    await create_task(
        task_id=task_id, user_id=current_user.sub,
        query=clean_query, original_query=body.query,
    )
    await audit_logger.record(
        AuditEvent.TASK_CREATED, detail="Task created",
        task_id=task_id, user_id=current_user.sub,
    )

    # ── Fire pipeline in background ────────────────────────────────────────────
    session_id = body.session_id or current_user.sub
    background_tasks.add_task(
        _run_pipeline_bg,
        task_id=task_id,
        query=clean_query,
        user_id=current_user.sub,
        session_id=session_id,
    )

    return TaskResponse(
        task_id=task_id,
        status="queued",
        message="Pipeline started. Stream live progress or poll for status.",
        stream_url=f"/tasks/{task_id}/stream",
    )


async def _run_pipeline_bg(task_id: str, query: str,
                            user_id: str, session_id: str) -> None:
    """Background task: runs full pipeline with retry."""
    def on_event(event) -> None:
        _push_event(task_id, event.stage, event.message, **event.data)

    from src.core.pipeline import run_with_retry, NexusPipeline
    pipeline = NexusPipeline()
    try:
        await pipeline.run(
            task_id=task_id, query=query,
            user_id=user_id, session_id=session_id,
            emit_event=on_event,
        )
        _push_event(task_id, "done", "Pipeline complete")
    except Exception as exc:
        log.error("background_pipeline_failed", task_id=task_id, error=str(exc))
        _push_event(task_id, "error", str(exc))
        # Retry via run_with_retry
        await run_with_retry(task_id, query, user_id)


@router.get("/{task_id}/stream")
async def stream_task(task_id: str, current_user: AuthUser, request: Request):
    """
    Server-Sent Events stream for live pipeline progress.
    Client connects and receives events as they happen.
    """
    task = await get_task_by_id(task_id)
    if not task or task["user_id"] != current_user.sub:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        sent = 0
        while True:
            if await request.is_disconnected():
                break
            events = _sse_events.get(task_id, [])
            for evt in events[sent:]:
                data = json.dumps({"stage": evt["stage"], "msg": evt["msg"], "ts": evt["ts"]})
                yield f"data: {data}\n\n"
                sent += 1
            # Check if task finished
            if any(e["stage"] in ("done","error") for e in events):
                yield "data: {\"stage\":\"close\"}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/", response_model=list[TaskStatusResponse])
async def list_tasks(current_user: AuthUser, limit: int = 20):
    tasks = await list_tasks_for_user(current_user.sub, limit=limit)
    return tasks


@router.get("/dead-letter", response_model=list[dict])
async def get_dlq(current_user: AuthUser, limit: int = 20):
    """View dead-letter queue (failed tasks that exhausted retries)."""
    return await get_dead_letter_queue(limit)


@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task(task_id: str, current_user: AuthUser):
    task = await get_task_by_id(task_id)
    if not task or task["user_id"] != current_user.sub:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
