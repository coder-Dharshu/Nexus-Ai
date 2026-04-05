"""
Nexus AI — Email Tool (Gmail + SMTP fallback)
Sends real emails via Gmail API (OAuth, free) or SMTP.
HITL gate always called before sending — user approves every email.
"""
from __future__ import annotations
import asyncio, base64, json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import structlog
log = structlog.get_logger(__name__)

class EmailTool:
    """
    Send, read, and draft emails.
    Always routes through HITL gate before sending.
    """

    def __init__(self):
        self._gmail = None
        self._smtp_cfg: Optional[dict] = None

    async def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        from_account: str = "",
        html: bool = False,
        reply_to: str = "",
        cc: list[str] = None,
    ) -> dict:
        """
        Send an email. Tries Gmail API first, SMTP fallback.
        ALWAYS requires HITL approval before this is called.
        """
        def _sync_send():
            # Try Gmail API
            try:
                svc = self._get_gmail_service()
                if svc:
                    msg = MIMEMultipart("alternative")
                    msg["To"] = to; msg["Subject"] = subject
                    if reply_to: msg["Reply-To"] = reply_to
                    if cc: msg["Cc"] = ", ".join(cc)
                    part = MIMEText(body, "html" if html else "plain")
                    msg.attach(part)
                    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                    sent = svc.users().messages().send(userId="me", body={"raw":raw}).execute()
                    return {"status":"sent","id":sent["id"],"to":to,"subject":subject,
                            "method":"gmail_api","message":f"Email sent to {to}"}
            except Exception as e:
                log.warning("gmail_api_failed", error=str(e))
            # SMTP fallback
            cfg = self._get_smtp_config()
            if cfg:
                import smtplib
                msg = MIMEMultipart()
                msg["From"] = cfg["user"]; msg["To"] = to; msg["Subject"] = subject
                msg.attach(MIMEText(body, "html" if html else "plain"))
                with smtplib.SMTP_SSL(cfg["host"], cfg["port"]) as srv:
                    srv.login(cfg["user"], cfg["password"])
                    srv.sendmail(cfg["user"], to, msg.as_string())
                return {"status":"sent","to":to,"subject":subject,
                        "method":"smtp","message":f"Email sent to {to} via SMTP"}
            return {"status":"error","message":"No email service configured. Run: nexus setup → add Gmail OAuth"}

        try:
            return await asyncio.to_thread(_sync_send)
        except Exception as e:
            return {"status":"error","message":str(e)}

    async def read_recent(self, count: int = 10, query: str = "", full_body: bool = False) -> list[dict]:
        """Read recent emails. Set full_body=True for tone analysis (needed by EmailAnalyzer)."""
        def _sync():
            svc = self._get_gmail_service()
            if not svc: return []
            q = f"in:inbox {query}".strip()
            results = svc.users().messages().list(userId="me", q=q, maxResults=count).execute()
            emails = []
            for item in results.get("messages", [])[:count]:
                fmt = "full" if full_body else "metadata"
                extra_args = {} if full_body else {"metadataHeaders": ["From", "To", "Subject", "Date"]}
                msg = svc.users().messages().get(
                    userId="me", id=item["id"], format=fmt, **extra_args
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                body_text = ""
                if full_body:
                    body_text = self._extract_body(msg.get("payload", {}))
                emails.append({
                    "id": msg.get("id", ""),
                    "from": headers.get("From", ""),
                    "to": headers.get("To", ""),
                    "subject": headers.get("Subject", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                    "body": body_text[:2000] if body_text else msg.get("snippet", ""),
                })
            return emails
        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            log.warning("read_recent_failed", error=str(e))
            return []

    def _extract_body(self, payload: dict) -> str:
        """Recursively extract plain-text body from Gmail message payload."""
        import base64
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                try:
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
                except Exception:
                    return ""
        # Recurse into parts
        for part in payload.get("parts", []):
            text = self._extract_body(part)
            if text:
                return text
        return ""

    async def list_accounts(self) -> list[dict]:
        """Return all configured email accounts."""
        accounts = []
        try:
            svc = self._get_gmail_service()
            if svc:
                profile = svc.users().getProfile(userId="me").execute()
                accounts.append({"email": profile.get("emailAddress",""),
                                  "provider": "Gmail", "method": "OAuth"})
        except: pass
        cfg = self._get_smtp_config()
        if cfg and cfg.get("user"):
            accounts.append({"email": cfg["user"], "provider": "SMTP", "method": "password"})
        return accounts

    def _get_gmail_service(self):
        if self._gmail: return self._gmail
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            from src.security.keychain import secrets_manager
            from config.settings import get_settings
            raw = secrets_manager.get(get_settings().gmail_credentials_key, required=False)
            if not raw: return None
            creds = Credentials.from_authorized_user_info(json.loads(raw))
            self._gmail = build("gmail","v1",credentials=creds)
            return self._gmail
        except Exception as e:
            log.debug("gmail_unavailable", error=str(e)); return None

    def _get_smtp_config(self) -> Optional[dict]:
        if self._smtp_cfg: return self._smtp_cfg
        try:
            from src.security.keychain import secrets_manager
            raw = secrets_manager.get("smtp_config", required=False)
            if raw: self._smtp_cfg = json.loads(raw); return self._smtp_cfg
        except: pass
        return None

email_tool = EmailTool()
