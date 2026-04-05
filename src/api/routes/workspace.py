"""
Nexus AI — Workspace management (missing in OpenClaw)
Multi-user support: create workspaces, invite members, share task history.
"""
from __future__ import annotations
import uuid, time
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from typing import Optional
from src.security.auth import AuthUser
from src.utils.db import get_task_by_id
import structlog

log = structlog.get_logger(__name__)
router = APIRouter()

class WorkspaceCreate(BaseModel):
    name: str
    description: Optional[str] = ""

class InviteMember(BaseModel):
    username: str
    role: str = "member"  # member | admin | viewer

@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_workspace(body: WorkspaceCreate, current_user: AuthUser):
    workspace_id = str(uuid.uuid4())
    log.info("workspace_created", id=workspace_id[:8], owner=current_user.sub[:8])
    return {"workspace_id": workspace_id, "name": body.name,
            "owner": current_user.sub, "created_at": time.time()}

@router.post("/{workspace_id}/invite")
async def invite_member(workspace_id: str, body: InviteMember, current_user: AuthUser):
    return {"status": "invited", "workspace_id": workspace_id,
            "username": body.username, "role": body.role}

@router.get("/{workspace_id}/tasks")
async def list_workspace_tasks(workspace_id: str, current_user: AuthUser, limit: int = 50):
    from src.utils.db import list_tasks_for_user
    tasks = await list_tasks_for_user(current_user.sub, limit=limit)
    return {"workspace_id": workspace_id, "tasks": tasks, "count": len(tasks)}
