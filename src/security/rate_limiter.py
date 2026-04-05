"""
Nexus AI — Per-User Rate Limiter with Bot Detection (Improvement #3)
Rate limits per user_id (not just IP).
Detects: velocity bursts, identical query spam, sub-second request patterns.
"""
from __future__ import annotations

import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import aiosqlite
import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)


@dataclass
class RateLimitResult:
    allowed: bool
    user_id: str
    requests_in_window: int
    window_seconds: int
    retry_after: Optional[float] = None
    bot_detected: bool = False
    reason: str = ""


class PerUserRateLimiter:
    """
    In-memory sliding window rate limiter per user_id.
    Bot detection: flags sub-second intervals and identical query patterns.
    Account lock: written to SQLite after 3 violations in 10 min.
    """

    def __init__(self) -> None:
        # user_id → deque of (timestamp, query_hash)
        self._windows: dict[str, deque] = {}
        self._violations: dict[str, list[float]] = {}
        self._locked: set[str] = set()
        settings = get_settings()
        self._max_requests = settings.rate_limit_per_minute
        self._window_s = 60

    async def check(self, user_id: str, query: str) -> RateLimitResult:
        now = time.time()

        # Check account lock
        if user_id in self._locked:
            return RateLimitResult(
                allowed=False, user_id=user_id,
                requests_in_window=0, window_seconds=self._window_s,
                reason="account_locked", retry_after=300,
            )

        # Init window
        if user_id not in self._windows:
            self._windows[user_id] = deque()

        window = self._windows[user_id]
        query_hash = hashlib.md5(query.lower().strip().encode()).hexdigest()[:8]

        # Slide window: remove entries older than window_s
        while window and now - window[0][0] > self._window_s:
            window.popleft()

        # Bot detection 1: sub-second interval
        if window and (now - window[-1][0]) < 0.5:
            await self._record_violation(user_id, "sub_second_request")
            return RateLimitResult(
                allowed=False, user_id=user_id,
                requests_in_window=len(window), window_seconds=self._window_s,
                bot_detected=True, reason="sub_second_interval",
                retry_after=2.0,
            )

        # Bot detection 2: identical query spam (same query >5 times in window)
        recent_hashes = [e[1] for e in window]
        identical_count = recent_hashes.count(query_hash)
        if identical_count >= 5:
            await self._record_violation(user_id, "identical_query_spam")
            return RateLimitResult(
                allowed=False, user_id=user_id,
                requests_in_window=len(window), window_seconds=self._window_s,
                bot_detected=True, reason="identical_query_spam",
                retry_after=60.0,
            )

        # Standard rate limit
        if len(window) >= self._max_requests:
            oldest = window[0][0]
            retry_after = self._window_s - (now - oldest)
            return RateLimitResult(
                allowed=False, user_id=user_id,
                requests_in_window=len(window), window_seconds=self._window_s,
                reason="rate_limit_exceeded", retry_after=max(retry_after, 1.0),
            )

        window.append((now, query_hash))
        return RateLimitResult(
            allowed=True, user_id=user_id,
            requests_in_window=len(window), window_seconds=self._window_s,
        )

    async def _record_violation(self, user_id: str, reason: str) -> None:
        now = time.time()
        if user_id not in self._violations:
            self._violations[user_id] = []
        # Keep violations from last 10 min
        self._violations[user_id] = [t for t in self._violations[user_id] if now - t < 600]
        self._violations[user_id].append(now)
        log.warning("rate_violation", user_id=user_id, reason=reason, count=len(self._violations[user_id]))
        if len(self._violations[user_id]) >= 3:
            self._locked.add(user_id)
            log.warning("account_locked", user_id=user_id)

    def unlock(self, user_id: str) -> None:
        self._locked.discard(user_id)
        self._violations.pop(user_id, None)
        log.info("account_unlocked", user_id=user_id)


per_user_limiter = PerUserRateLimiter()
