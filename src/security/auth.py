"""
Nexus AI — Authentication layer.

JWT-based auth. Secret lives in OS keychain (never .env).
Every endpoint requires a valid Bearer token — no unauthenticated state exists.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Annotated, Optional

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from config.settings import get_settings
from src.security.keychain import secrets_manager

log = structlog.get_logger(__name__)

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer = HTTPBearer(auto_error=True)


# ── Models ────────────────────────────────────────────────────────────────────

class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class TokenData(BaseModel):
    sub: str           # user identifier
    iat: int           # issued at (unix)
    exp: int           # expiry (unix)
    jti: str           # unique token id (for revocation)
    scope: str = "access"


class UserCredentials(BaseModel):
    username: str
    password: str


# ── Password hashing ──────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _get_jwt_secret() -> str:
    settings = get_settings()
    return secrets_manager.ensure_jwt_secret(settings.jwt_keychain_username)


def create_token_pair(user_id: str) -> TokenPair:
    """
    Issue a (access_token, refresh_token) pair for a user.
    Both tokens signed with the keychain-stored secret.
    """
    settings = get_settings()
    secret = _get_jwt_secret()
    now = int(time.time())

    access_jti = secrets_manager.generate_strong_secret(16)
    refresh_jti = secrets_manager.generate_strong_secret(16)

    access_exp = now + settings.jwt_access_token_expire_minutes * 60
    refresh_exp = now + settings.jwt_refresh_token_expire_days * 86400

    access_payload = {
        "sub": user_id,
        "iat": now,
        "exp": access_exp,
        "jti": access_jti,
        "scope": "access",
    }
    refresh_payload = {
        "sub": user_id,
        "iat": now,
        "exp": refresh_exp,
        "jti": refresh_jti,
        "scope": "refresh",
    }

    access_token = jwt.encode(access_payload, secret, algorithm=settings.jwt_algorithm)
    refresh_token = jwt.encode(refresh_payload, secret, algorithm=settings.jwt_algorithm)

    log.info("token_pair_issued", user_id=user_id, access_jti=access_jti)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


def decode_token(token: str, *, expected_scope: str = "access") -> TokenData:
    """
    Decode and fully validate a JWT.
    Raises HTTPException 401 on any failure — never leaks token internals.
    """
    settings = get_settings()
    secret = _get_jwt_secret()

    try:
        payload = jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])
    except ExpiredSignatureError:
        log.warning("token_expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError as exc:
        log.warning("token_invalid", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("scope") != expected_scope:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong token scope",
        )

    return TokenData(**payload)


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    request: Request,
) -> TokenData:
    """
    FastAPI dependency — inject into any protected route.
    Validates JWT, logs the request.
    """
    token_data = decode_token(credentials.credentials)
    log.info(
        "authenticated_request",
        user=token_data.sub,
        path=request.url.path,
        method=request.method,
    )
    return token_data


# Type alias for clean route signatures
AuthUser = Annotated[TokenData, Depends(require_auth)]


# ── CSRF ──────────────────────────────────────────────────────────────────────

class CSRFManager:
    """
    Double-submit cookie CSRF protection.
    Token is HMAC-signed with the JWT secret so it cannot be forged.
    """

    def __init__(self) -> None:
        self._secret: Optional[str] = None

    def _key(self) -> bytes:
        if self._secret is None:
            self._secret = _get_jwt_secret()
        return self._secret.encode()

    def generate_token(self, session_id: str) -> str:
        ts = str(int(time.time()))
        msg = f"{session_id}:{ts}".encode()
        sig = hmac.new(self._key(), msg, hashlib.sha256).hexdigest()
        return f"{ts}:{sig}"

    def validate_token(self, session_id: str, token: str) -> bool:
        settings = get_settings()
        try:
            ts_str, sig = token.split(":", 1)
            age = int(time.time()) - int(ts_str)
            if age > settings.csrf_token_expire_seconds:
                return False
            msg = f"{session_id}:{ts_str}".encode()
            expected = hmac.new(self._key(), msg, hashlib.sha256).hexdigest()
            return hmac.compare_digest(sig, expected)
        except Exception:
            return False


csrf_manager = CSRFManager()
