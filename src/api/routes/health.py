"""Nexus AI — Health check endpoints."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    version: str
    binding: str


@router.get("/ping", response_model=HealthResponse)
async def ping():
    """Basic liveness check. No auth required."""
    return HealthResponse(
        status="ok",
        version="0.1.0",
        binding="127.0.0.1 (secure)",
    )


@router.get("/ready")
async def ready():
    """Readiness check — confirms DB and keychain are accessible."""
    from src.security.keychain import secrets_manager
    from src.utils.db import check_db_connection

    db_ok = await check_db_connection()
    keychain_ok = True
    try:
        secrets_manager.ensure_jwt_secret()
    except Exception:
        keychain_ok = False

    if not db_ok or not keychain_ok:
        from fastapi import status
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not_ready", "db": db_ok, "keychain": keychain_ok},
        )
    return {"status": "ready", "db": db_ok, "keychain": keychain_ok}
