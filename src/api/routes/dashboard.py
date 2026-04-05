"""
Nexus AI — Dashboard API routes.

Read-only endpoints for the Streamlit security dashboard.
All data comes from audit_log (read-only connection — no agents here).
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.security.audit_logger import AuditEvent, audit_logger
from src.security.auth import AuthUser
from src.utils.db import get_task_by_id, list_tasks_for_user

router = APIRouter()


class DashboardSummary(BaseModel):
    tasks_total: int
    tasks_completed: int
    tasks_pending_approval: int
    injection_attempts_24h: int
    browser_blocks_24h: int
    sources_scraped_24h: int


@router.get("/summary", response_model=DashboardSummary)
async def get_summary(current_user: AuthUser):
    tasks = await list_tasks_for_user(current_user.sub, limit=200)
    completed = sum(1 for t in tasks if t["status"] == "completed")
    pending = sum(1 for t in tasks if t["status"] == "pending_approval")
    injections = await audit_logger.count_by_event(AuditEvent.INPUT_BLOCKED, since_seconds=86400)
    blocks = await audit_logger.count_by_event(AuditEvent.BROWSER_BLOCKED, since_seconds=86400)
    scraped = await audit_logger.count_by_event(AuditEvent.BROWSER_SCRAPED, since_seconds=86400)
    return DashboardSummary(
        tasks_total=len(tasks),
        tasks_completed=completed,
        tasks_pending_approval=pending,
        injection_attempts_24h=injections,
        browser_blocks_24h=blocks,
        sources_scraped_24h=scraped,
    )


@router.get("/audit-log")
async def get_audit_log(current_user: AuthUser, limit: int = 50):
    entries = await audit_logger.get_recent(limit=limit)
    return {"entries": entries, "count": len(entries)}


@router.get("/tasks")
async def get_tasks(current_user: AuthUser, limit: int = 50):
    tasks = await list_tasks_for_user(current_user.sub, limit=limit)
    return {"tasks": tasks}
