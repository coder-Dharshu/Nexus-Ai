"""
Nexus AI — Auth routes.

POST /auth/login    — username + password → JWT pair
POST /auth/refresh  — refresh token → new access token
POST /auth/logout   — invalidate session (client-side token drop)
GET  /auth/me       — return current user info
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from config.settings import get_settings
from src.security.audit_logger import AuditEvent, audit_logger
from src.security.auth import (
    AuthUser,
    TokenPair,
    UserCredentials,
    create_token_pair,
    decode_token,
    hash_password,
    verify_password,
)
from src.security.keychain import secrets_manager
from src.utils.db import get_user_by_username, update_last_login

router = APIRouter()
settings = get_settings()


class RefreshRequest(BaseModel):
    refresh_token: str


class MeResponse(BaseModel):
    user_id: str
    username: str
    last_login: str | None


@router.post("/login", response_model=TokenPair)
async def login(credentials: UserCredentials, request: Request):
    """
    Exchange username + password for a JWT token pair.
    Timing-safe comparison to prevent user enumeration.
    """
    user = await get_user_by_username(credentials.username)

    # Always run verify_password even for missing users (timing-safe)
    dummy_hash = "$2b$12$dummy.hash.to.prevent.timing.attacks.on.username.enum"
    password_ok = verify_password(
        credentials.password,
        user["password_hash"] if user else dummy_hash,
    )

    if not user or not password_ok:
        await audit_logger.record(
            AuditEvent.AUTH_FAILURE,
            detail=f"Failed login attempt for username: {credentials.username[:20]}",
            severity="WARNING",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tokens = create_token_pair(user["id"])
    await update_last_login(user["id"])
    await audit_logger.record(
        AuditEvent.AUTH_SUCCESS,
        detail="User logged in",
        user_id=user["id"],
    )
    return tokens


@router.post("/refresh", response_model=TokenPair)
async def refresh_token(body: RefreshRequest):
    """Exchange a valid refresh token for a new access token pair."""
    token_data = decode_token(body.refresh_token, expected_scope="refresh")
    tokens = create_token_pair(token_data.sub)
    await audit_logger.record(
        AuditEvent.TOKEN_ISSUED,
        detail="Token refreshed",
        user_id=token_data.sub,
    )
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(current_user: AuthUser):
    """
    Stateless logout — client drops tokens.
    Server logs the event. For full revocation, add a token blacklist.
    """
    await audit_logger.record(
        AuditEvent.AUTH_SUCCESS,
        detail="User logged out",
        user_id=current_user.sub,
    )


@router.get("/me", response_model=MeResponse)
async def get_me(current_user: AuthUser):
    """Return the current authenticated user's info."""
    user = await get_user_by_username(current_user.sub)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return MeResponse(
        user_id=user["id"],
        username=user["username"],
        last_login=user.get("last_login"),
    )
