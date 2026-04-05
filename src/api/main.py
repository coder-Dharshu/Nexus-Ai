"""
Nexus AI v2.0 — Production FastAPI Application
Supports local (127.0.0.1) and cloud deployment (0.0.0.0 behind reverse proxy).
Security: CORS, HSTS, body size limit, rate limiting, JWT on every route.
"""
from __future__ import annotations
import os, time
from contextlib import asynccontextmanager
import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from config.settings import get_settings
from src.api.routes import auth, tasks, health
from src.security.audit_logger import AuditEvent, audit_logger
from src.utils.db import init_databases
from src.utils.logger import setup_logging

log = structlog.get_logger(__name__)
settings = get_settings()

def _get_key(request: Request) -> str:
    user = getattr(request.state, "user_id", None)
    return user or get_remote_address(request)

limiter = Limiter(key_func=_get_key,
                  default_limits=[f"{settings.rate_limit_per_minute}/minute"])

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("nexus_starting", host=settings.host, port=settings.port, env=settings.environment)

    # In production cloud deployment, host can be 0.0.0.0 (behind reverse proxy / container)
    # Locally, enforce 127.0.0.1 only
    if settings.environment == "development" and settings.host == "0.0.0.0":
        log.warning("dev_host_warning",
                    msg="Running on 0.0.0.0 in development. Add a reverse proxy for production.")

    await init_databases()

    try:
        from src.agents.llm_client import llm_client
        health_result = await llm_client.health_check()
        log.info("llm_health", **health_result)
    except Exception as exc:
        log.warning("llm_health_check_failed", error=str(exc))

    try:
        from src.scheduler.jobs import setup_scheduler
        setup_scheduler()
    except Exception as exc:
        log.warning("scheduler_start_failed", error=str(exc))

    try:
        from src.interfaces.telegram_bot import nexus_bot
        await nexus_bot.start_polling()
    except Exception as exc:
        log.warning("telegram_polling_start_failed", error=str(exc))

    await audit_logger.record(AuditEvent.SYSTEM_START, detail="Nexus AI v2.0 started")
    log.info("nexus_ready", version="2.0.0")
    yield

    try:
        from src.interfaces.telegram_bot import nexus_bot
        await nexus_bot.stop_polling()
    except Exception as exc:
        log.warning("telegram_polling_stop_failed", error=str(exc))

    await audit_logger.record(AuditEvent.SYSTEM_STOP, detail="Nexus AI stopped")
    log.info("nexus_stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Nexus AI",
        version="2.0.0",
        description="Multi-agent AI with live data verification and debate convergence",
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"],
        max_age=600,
    )

    @app.middleware("http")
    async def body_size_limit(request: Request, call_next):
        cl = int(request.headers.get("content-length", "0"))
        if cl > settings.max_request_body_kb * 1024:
            return JSONResponse(status_code=413, content={"detail": "Request too large"})
        return await call_next(request)

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        import uuid
        rid = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        request.state.request_id = rid
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.middleware("http")
    async def add_timing(request: Request, call_next):
        t0 = time.perf_counter()
        response: Response = await call_next(request)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        response.headers["X-Response-Time-Ms"] = str(ms)
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response

    app.include_router(health.router, prefix="/health",     tags=["health"])
    app.include_router(auth.router,   prefix="/auth",       tags=["auth"])
    app.include_router(tasks.router,  prefix="/tasks",      tags=["tasks"])

    from src.api.routes import stream, workspace, insights
    app.include_router(stream.router,     prefix="/stream",     tags=["stream"])
    app.include_router(workspace.router,  prefix="/workspace",  tags=["workspace"])
    app.include_router(insights.router,   prefix="/insights",   tags=["insights"])

    # Serve web UI
    static_dir = Path(__file__).parent.parent.parent / "src" / "interfaces"
    if static_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root():
        return {"name": "Nexus AI", "version": "2.0.0",
                "status": "running", "docs": "/docs" if settings.debug else "disabled"}

    return app

app = create_app()
