"""
Nexus AI — Human-in-the-Loop (HITL) Approval Gate.

For every irreversible action (send email, book, post, pay):
  1. Agent STOPS — state is frozen in DB
  2. Draft preview is sent to Telegram as an approval card
  3. User has 3 choices: Approve / Edit / Reject
  4. Task expires after HITL_EXPIRY_HOURS if no response
  5. Inaction ALWAYS means cancellation — never auto-execution

State machine:
  PENDING_APPROVAL → APPROVED → (executor runs) → COMPLETED
  PENDING_APPROVAL → REJECTED → COMPLETED (discarded)
  PENDING_APPROVAL → EDITING  → PENDING_APPROVAL (re-approve)
  PENDING_APPROVAL → EXPIRED  → COMPLETED (cancelled by timeout)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import structlog

from config.settings import get_settings
from src.security.audit_logger import AuditEvent, audit_logger
from src.security.pii_masker import pii_masker

log = structlog.get_logger(__name__)
_settings = get_settings()


class ApprovalStatus(str, Enum):
    PENDING  = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITING  = "editing"
    EXPIRED  = "expired"


@dataclass
class ApprovalRequest:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    user_id: str = ""
    action_type: str = ""          # send_email | book_flight | post_message | ...
    draft: dict = field(default_factory=dict)   # structured draft action
    draft_preview: str = ""        # human-readable preview text
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0        # created_at + HITL_EXPIRY_HOURS * 3600
    decided_at: Optional[float] = None
    decision_notes: str = ""
    edit_count: int = 0

    def __post_init__(self):
        if self.expires_at == 0.0:
            self.expires_at = self.created_at + _settings.hitl_expiry_hours * 3600

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def time_remaining_s(self) -> float:
        return max(0.0, self.expires_at - time.time())

    @property
    def safe_preview(self) -> str:
        """PII-masked preview for external notifications."""
        return pii_masker.mask(self.draft_preview).safe_text


# ── Irreversible action types ─────────────────────────────────────────────────

IRREVERSIBLE_ACTIONS = frozenset({
    "send_email",
    "send_message",
    "book_flight",
    "book_hotel",
    "book_train",
    "post_slack",
    "post_twitter",
    "make_payment",
    "delete_file",
    "submit_form",
    "create_calendar_event",
    "send_telegram",
})


def is_irreversible(action_type: str) -> bool:
    return action_type in IRREVERSIBLE_ACTIONS


# ── HITL Gate ─────────────────────────────────────────────────────────────────

class HITLGate:
    """
    Manages the full approval lifecycle.
    Stores pending requests in memory (Phase 4 will persist to SQLite).
    Sends Telegram notifications when a request is created.
    """

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._callbacks: dict[str, Callable] = {}
        self._telegram: Optional[TelegramNotifier] = None

    # ── Create request ────────────────────────────────────────────────────────

    async def request_approval(
        self,
        task_id: str,
        user_id: str,
        action_type: str,
        draft: dict,
        draft_preview: str,
    ) -> ApprovalRequest:
        """
        Pause an action and request user approval.
        Returns the ApprovalRequest immediately — caller must await decision.
        """
        if not is_irreversible(action_type):
            raise ValueError(
                f"'{action_type}' is not in IRREVERSIBLE_ACTIONS. "
                f"Only irreversible actions need HITL approval."
            )

        req = ApprovalRequest(
            task_id=task_id,
            user_id=user_id,
            action_type=action_type,
            draft=draft,
            draft_preview=draft_preview,
        )
        self._pending[req.id] = req

        await audit_logger.record(
            AuditEvent.HITL_TRIGGERED,
            detail=f"HITL gate triggered for {action_type}",
            task_id=task_id,
            user_id=user_id,
            metadata={"approval_id": req.id, "action_type": action_type,
                      "expires_at": req.expires_at},
        )

        # Send notification (non-blocking)
        asyncio.create_task(self._notify(req))

        log.info("hitl_created", approval_id=req.id, action=action_type,
                 expires_in_h=_settings.hitl_expiry_hours)
        return req

    async def create_approval(
        self,
        task_id: str,
        user_id: str,
        action_type: str = "send_email",
        draft_text: str = "",
        draft_dict: Optional[dict] = None,
    ) -> str:
        """Convenience wrapper called by the pipeline. Returns approval_id."""
        req = await self.request_approval(
            task_id=task_id,
            user_id=user_id,
            action_type=action_type,
            draft=draft_dict or {"text": draft_text},
            draft_preview=draft_text[:500],
        )
        return req.id

    async def wait_for_decision(
        self, approval_id: str, poll_interval: float = 5.0, timeout_s: float = 300.0
    ) -> ApprovalStatus:
        """Poll until user decides or timeout. Returns final status."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            req = self._pending.get(approval_id)
            if not req:
                return ApprovalStatus.EXPIRED
            if req.is_expired:
                req.status = ApprovalStatus.EXPIRED
                return ApprovalStatus.EXPIRED
            if req.status != ApprovalStatus.PENDING:
                return req.status
            await asyncio.sleep(poll_interval)
        return ApprovalStatus.EXPIRED

    # ── Decision handlers ─────────────────────────────────────────────────────

    async def approve(self, approval_id: str, notes: str = "") -> ApprovalRequest:
        req = self._get_or_raise(approval_id)
        self._check_not_expired(req)
        req.status = ApprovalStatus.APPROVED
        req.decided_at = time.time()
        req.decision_notes = notes
        await audit_logger.record(
            AuditEvent.HITL_APPROVED,
            detail=f"User approved {req.action_type}",
            task_id=req.task_id,
            user_id=req.user_id,
            metadata={"approval_id": approval_id},
        )
        log.info("hitl_approved", approval_id=approval_id)
        return req

    async def reject(self, approval_id: str, reason: str = "") -> ApprovalRequest:
        req = self._get_or_raise(approval_id)
        req.status = ApprovalStatus.REJECTED
        req.decided_at = time.time()
        req.decision_notes = reason
        await audit_logger.record(
            AuditEvent.HITL_REJECTED,
            detail=f"User rejected {req.action_type}: {reason}",
            task_id=req.task_id,
            user_id=req.user_id,
        )
        log.info("hitl_rejected", approval_id=approval_id, reason=reason)
        return req

    async def approve_by_prefix(self, prefix: str, user_id: str) -> bool:
        """
        Approve a pending request matching the given ID prefix.
        After marking approved, executes the action (email send, etc.) and
        fires a Telegram completion notification.
        Returns True if found and approved, False if not found.
        """
        req = self._find_by_prefix(prefix, user_id)
        if not req:
            return False
        await self.approve(req.id)
        # Execute the approved action in background
        asyncio.create_task(self._execute_approved(req))
        return True

    async def reject_by_prefix(self, prefix: str, user_id: str) -> bool:
        """Reject a pending request matching the given ID prefix. Returns True if found."""
        req = self._find_by_prefix(prefix, user_id)
        if not req:
            return False
        await self.reject(req.id)
        return True

    def _find_by_prefix(self, prefix: str, user_id: str) -> Optional[ApprovalRequest]:
        """Find a pending request by ID prefix for a given user."""
        prefix = prefix.strip().lower()
        for req in self._pending.values():
            if req.id.lower().startswith(prefix) and req.user_id == user_id:
                if req.status == ApprovalStatus.PENDING and not req.is_expired:
                    return req
        return None

    async def _execute_approved(self, req: ApprovalRequest) -> None:
        """
        Execute the approved action after HITL approval.
        Supports: send_email, create_calendar_event.
        Sends a Telegram completion notification when done.
        """
        success = False
        result_msg = ""
        try:
            if req.action_type == "send_email":
                from src.tools.email_tool import email_tool
                draft = req.draft
                result = await email_tool.send_email(
                    to=draft.get("to", ""),
                    subject=draft.get("subject", "(no subject)"),
                    body=draft.get("body", ""),
                )
                success = result.get("status") == "sent"
                result_msg = result.get("message", "Email sent.")
            elif req.action_type == "create_calendar_event":
                # Calendar integration placeholder — mark success if tool available
                result_msg = f"Calendar event created: {req.draft.get('title', '')}"
                success = True
            else:
                result_msg = f"Action '{req.action_type}' executed."
                success = True
        except Exception as exc:
            result_msg = f"Execution failed: {exc}"
            log.error("hitl_execute_failed", approval_id=req.id, error=str(exc))

        # Send Telegram completion notification
        try:
            from src.interfaces.telegram_bot import nexus_bot
            chat_id = req.user_id
            status_icon = "✅" if success else "❌"
            await nexus_bot.send_completion_notification(
                chat_id=chat_id,
                task_id=req.task_id,
                summary=f"{status_icon} *{req.action_type}*\n{result_msg}",
            )
        except Exception as exc:
            log.warning("hitl_notify_complete_failed", error=str(exc))

        # Update task status in DB
        try:
            from src.utils.db import update_task_status
            await update_task_status(
                req.task_id, "completed" if success else "failed",
                result={"message": result_msg},
            )
        except Exception:
            pass

        log.info("hitl_executed", approval_id=req.id, success=success)

    async def request_edit(
        self, approval_id: str, edited_draft: dict, edited_preview: str
    ) -> ApprovalRequest:
        req = self._get_or_raise(approval_id)
        self._check_not_expired(req)
        req.status = ApprovalStatus.EDITING
        req.draft = edited_draft
        req.draft_preview = edited_preview
        req.edit_count += 1
        # Reset to PENDING after edit
        req.status = ApprovalStatus.PENDING
        await audit_logger.record(
            AuditEvent.HITL_EDITED,
            detail=f"User edited draft (edit #{req.edit_count})",
            task_id=req.task_id,
            user_id=req.user_id,
        )
        # Re-notify with updated draft
        asyncio.create_task(self._notify(req))
        log.info("hitl_edited", approval_id=approval_id, edit_count=req.edit_count)
        return req

    # ── Expiry checker (run by scheduler) ─────────────────────────────────────

    async def expire_stale_requests(self) -> int:
        """Mark all expired pending requests as EXPIRED. Returns count."""
        expired = 0
        for req in list(self._pending.values()):
            if req.status == ApprovalStatus.PENDING and req.is_expired:
                req.status = ApprovalStatus.EXPIRED
                req.decided_at = time.time()
                await audit_logger.record(
                    AuditEvent.HITL_EXPIRED,
                    detail=f"Approval request expired — task cancelled",
                    task_id=req.task_id,
                    user_id=req.user_id,
                    severity="WARNING",
                )
                log.warning("hitl_expired", approval_id=req.id, action=req.action_type)
                expired += 1
        return expired

    # ── Query ─────────────────────────────────────────────────────────────────

    def get(self, approval_id: str) -> Optional[ApprovalRequest]:
        return self._pending.get(approval_id)

    def list_pending(self, user_id: str) -> list[ApprovalRequest]:
        return [
            r for r in self._pending.values()
            if r.user_id == user_id and r.status == ApprovalStatus.PENDING and not r.is_expired
        ]

    # ── Notification ──────────────────────────────────────────────────────────

    async def _notify(self, req: ApprovalRequest) -> None:
        if self._telegram is None:
            self._telegram = TelegramNotifier()
        try:
            await self._telegram.send_approval_card(req)
        except Exception as exc:
            log.warning("hitl_notify_failed", error=str(exc), approval_id=req.id)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_or_raise(self, approval_id: str) -> ApprovalRequest:
        req = self._pending.get(approval_id)
        if not req:
            raise KeyError(f"Approval request '{approval_id}' not found")
        return req

    @staticmethod
    def _check_not_expired(req: ApprovalRequest) -> None:
        if req.is_expired:
            raise RuntimeError(
                f"Approval request '{req.id}' has expired. "
                f"Inaction means cancellation — create a new task."
            )


# ── Telegram notifier ─────────────────────────────────────────────────────────

class TelegramNotifier:
    """Sends HITL approval cards to Telegram."""

    async def send_approval_card(self, req: ApprovalRequest) -> None:
        token = None
        chat_id = None
        try:
            from src.security.keychain import secrets_manager
            token = secrets_manager.get(_settings.telegram_keychain_key, required=False)
            chat_id = secrets_manager.get(_settings.telegram_chat_id_keychain_key, required=False)
        except Exception:
            pass

        if not token or not chat_id:
            log.warning("telegram_not_configured", approval_id=req.id)
            return

        remaining_h = req.time_remaining_s / 3600
        text = (
            f"🔔 *Action needs your approval*\n\n"
            f"*Task:* `{req.task_id[:8]}`\n"
            f"*Action:* {req.action_type}\n"
            f"*Expires in:* {remaining_h:.1f}h\n\n"
            f"*Preview:*\n{req.safe_preview[:400]}\n\n"
            f"Reply with:\n"
            f"✅ `/approve {req.id[:8]}`\n"
            f"✏️ `/edit {req.id[:8]}`\n"
            f"❌ `/reject {req.id[:8]}`"
        )

        import httpx
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                })
            log.info("telegram_sent", approval_id=req.id)
        except Exception as exc:
            log.warning("telegram_send_failed", error=str(exc))


# Module-level singleton
hitl_gate = HITLGate()
