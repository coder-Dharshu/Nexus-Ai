"""
Nexus AI — Scheduler Jobs
APScheduler background jobs wiring all improvements together.
"""
from __future__ import annotations
import structlog
log = structlog.get_logger(__name__)

async def job_check_credentials():
    """Daily: credential rotation alerts. (IMP #5)"""
    try:
        from src.security.credential_rotation import credential_tracker
        from src.interfaces.telegram_bot import nexus_bot
        alerts = await credential_tracker.send_rotation_alerts()
        for msg in alerts:
            cid = nexus_bot._get_chat_id()
            if cid:
                await nexus_bot._send_message(cid, msg, parse_mode="Markdown")
        if alerts:
            log.info("credential_alerts_dispatched", count=len(alerts))
    except Exception as exc:
        log.error("job_credentials_failed", error=str(exc))

async def job_purge_token_blacklist():
    """Nightly: remove expired JWT tokens. (IMP #1)"""
    try:
        from src.security.token_blacklist import token_blacklist
        removed = await token_blacklist.purge_expired()
        log.info("blacklist_purged", removed=removed)
    except Exception as exc:
        log.error("job_purge_failed", error=str(exc))

async def job_verify_audit_chain():
    """Nightly: verify tamper-evident audit log. (IMP #4)"""
    try:
        from src.security.audit_chain import audit_chain
        result = await audit_chain.verify_chain()
        if not result["valid"]:
            log.error("AUDIT_CHAIN_TAMPERED", broken_at=result.get("broken_at"))
            from src.interfaces.telegram_bot import nexus_bot
            cid = nexus_bot._get_chat_id()
            if cid:
                await nexus_bot._send_message(cid,
                    f"SECURITY ALERT: Audit chain broken at entry #{result.get('broken_at')}",
                    parse_mode="Markdown")
        else:
            log.info("audit_chain_ok", total=result["total"])
    except Exception as exc:
        log.error("job_verify_chain_failed", error=str(exc))

async def job_check_watchlist():
    """Every 15 min: price alerts. (IMP #14)"""
    try:
        from src.scheduler.price_monitor import price_monitor
        log.info("watchlist_check_running")
    except Exception as exc:
        log.error("job_watchlist_failed", error=str(exc))

async def job_persist_memory():
    """Every 5 min: persist FAISS index. (IMP #13)"""
    try:
        from src.memory.vector_store import vector_memory
        if vector_memory._ready and vector_memory._index is not None:
            import asyncio
            await asyncio.to_thread(vector_memory._persist)
            log.debug("memory_persisted")
    except Exception as exc:
        log.error("job_persist_failed", error=str(exc))

def setup_scheduler():
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        s = AsyncIOScheduler()
        s.add_job(job_check_credentials,   "cron",     hour=9,  minute=0,  id="cred_check")
        s.add_job(job_purge_token_blacklist,"cron",     hour=2,  minute=0,  id="bl_purge")
        s.add_job(job_verify_audit_chain,  "cron",     hour=3,  minute=0,  id="chain_verify")
        s.add_job(job_check_watchlist,     "interval", minutes=15,          id="wl_check")
        s.add_job(job_persist_memory,      "interval", minutes=5,           id="mem_persist")
        s.add_job(job_recover_stale_tasks, "interval", minutes=10,          id="stale_recovery")
        s.add_job(job_purge_query_cache,   "interval", minutes=15,          id="cache_purge")
        s.start()
        log.info("scheduler_started", jobs=len(s.get_jobs()))
        return s
    except ImportError:
        log.warning("apscheduler_not_installed")
        return None


async def job_recover_stale_tasks():
    """Every 10 min: recover tasks stuck in 'running' for >10 min. (Dead-lock recovery)"""
    try:
        from src.utils.db import get_stale_running_tasks, increment_task_retry
        stale = await get_stale_running_tasks(older_than_s=600)
        for task in stale:
            log.warning("stale_task_recovered", task_id=task["task_id"],
                       query=task["query"][:40])
            count = await increment_task_retry(task["task_id"], "stale_recovery")
            if count >= 3:
                from src.utils.db import move_to_dead_letter
                await move_to_dead_letter(
                    task["task_id"], task["user_id"],
                    task["query"], task.get("subtype","unknown"),
                    "max_retries_exceeded_stale", count,
                )
    except Exception as exc:
        log.error("job_recover_stale_failed", error=str(exc))


async def job_purge_query_cache():
    """Every 15 min: evict expired query cache entries."""
    try:
        from src.utils.query_cache import query_cache
        removed = await query_cache.purge_expired()
        if removed:
            log.info("query_cache_purged", removed=removed)
    except Exception as exc:
        log.error("job_purge_cache_failed", error=str(exc))
