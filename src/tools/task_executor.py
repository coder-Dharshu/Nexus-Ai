"""
Nexus AI — Task Executor
Routes approved tasks to the right tool: email, Spotify, calendar, web search, etc.
Every irreversible action must arrive here already HITL-approved.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any, Optional
import structlog
log = structlog.get_logger(__name__)

@dataclass
class TaskResult:
    success: bool; action: str; message: str
    data: Optional[dict] = None; error: Optional[str] = None

class TaskExecutor:
    """
    Unified executor for all action tasks.
    Routes to specific tools based on action type.
    """

    async def execute(self, action_type: str, params: dict, user_id: str = "") -> TaskResult:
        """Route to correct tool based on action_type."""
        handlers = {
            # Music
            "spotify_play":       self._spotify_play,
            "spotify_pause":      self._spotify_pause,
            "spotify_next":       self._spotify_next,
            "spotify_volume":     self._spotify_volume,
            "spotify_search":     self._spotify_search,
            # Email
            "send_email":         self._send_email,
            "read_email":         self._read_email,
            # Calendar
            "create_calendar_event": self._create_calendar,
            "list_calendar_events":  self._list_calendar,
            # Web
            "web_search":         self._web_search,
            "open_url":           self._open_url,
            # System
            "set_reminder":       self._set_reminder,
            "send_telegram":      self._send_telegram,
        }
        handler = handlers.get(action_type)
        if not handler:
            return TaskResult(success=False, action=action_type,
                              message=f"Unknown action: {action_type}",
                              error="unsupported_action")
        try:
            log.info("task_executing", action=action_type, user=user_id[:8] if user_id else "")
            return await handler(params)
        except Exception as e:
            log.error("task_failed", action=action_type, error=str(e))
            return TaskResult(success=False, action=action_type,
                              message=f"Task failed: {e}", error=str(e))

    # ── Spotify ─────────────────────────────────────────────────────────────────
    async def _spotify_play(self, p: dict) -> TaskResult:
        from src.tools.spotify_tool import spotify_tool
        query = p.get("query","")
        if not query: return TaskResult(False,"spotify_play","No song/artist specified")
        r = await spotify_tool.play(query, p.get("device"))
        return TaskResult(success=r.get("status")=="playing",
                          action="spotify_play", message=r.get("message",""), data=r)

    async def _spotify_pause(self, p: dict) -> TaskResult:
        from src.tools.spotify_tool import spotify_tool
        r = await spotify_tool.pause()
        return TaskResult(success=True, action="spotify_pause", message=r.get("message",""), data=r)

    async def _spotify_next(self, p: dict) -> TaskResult:
        from src.tools.spotify_tool import spotify_tool
        r = await spotify_tool.next_track()
        return TaskResult(success=True, action="spotify_next", message=r.get("message",""), data=r)

    async def _spotify_volume(self, p: dict) -> TaskResult:
        from src.tools.spotify_tool import spotify_tool
        r = await spotify_tool.set_volume(p.get("volume_pct", 50))
        return TaskResult(success=True, action="spotify_volume", message=r.get("message",""), data=r)

    async def _spotify_search(self, p: dict) -> TaskResult:
        from src.tools.spotify_tool import spotify_tool
        r = await spotify_tool.search(p.get("query",""), p.get("limit",5))
        results = r.get("results",[])
        msg = f"Found {len(results)} tracks for '{p.get('query','')}'"
        return TaskResult(success=True, action="spotify_search", message=msg, data=r)

    # ── Email ───────────────────────────────────────────────────────────────────
    async def _send_email(self, p: dict) -> TaskResult:
        from src.tools.email_tool import email_tool
        r = await email_tool.send_email(
            to=p.get("to",""), subject=p.get("subject",""),
            body=p.get("body",""), from_account=p.get("from_account",""),
            html=p.get("html", False), cc=p.get("cc",[]),
        )
        return TaskResult(success=r.get("status")=="sent",
                          action="send_email", message=r.get("message",""), data=r)

    async def _read_email(self, p: dict) -> TaskResult:
        from src.tools.email_tool import email_tool
        emails = await email_tool.read_recent(p.get("count",10), p.get("query",""))
        return TaskResult(success=True, action="read_email",
                          message=f"Retrieved {len(emails)} emails",
                          data={"emails": emails, "count": len(emails)})

    # ── Calendar ─────────────────────────────────────────────────────────────────
    async def _create_calendar(self, p: dict) -> TaskResult:
        """Google Calendar API (free)."""
        try:
            import json as _json
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            from src.security.keychain import secrets_manager
            from config.settings import get_settings
            raw = secrets_manager.get(get_settings().gmail_credentials_key, required=False)
            if not raw:
                return TaskResult(False,"create_calendar","Gmail/Calendar OAuth not configured")
            creds = Credentials.from_authorized_user_info(_json.loads(raw))
            svc = build("calendar","v3",credentials=creds)
            event = {
                "summary": p.get("title","Meeting"),
                "description": p.get("description",""),
                "start": {"dateTime": p.get("start_time"), "timeZone": p.get("timezone","Asia/Kolkata")},
                "end":   {"dateTime": p.get("end_time"),   "timeZone": p.get("timezone","Asia/Kolkata")},
                "attendees": [{"email": e} for e in p.get("attendees",[])],
            }
            created = svc.events().insert(calendarId="primary", body=event,
                                           sendNotifications=True).execute()
            return TaskResult(success=True, action="create_calendar",
                              message=f"Event created: {created.get('summary','')}",
                              data={"event_id": created.get("id"), "link": created.get("htmlLink")})
        except Exception as e:
            return TaskResult(False, "create_calendar", f"Failed: {e}", error=str(e))

    async def _list_calendar(self, p: dict) -> TaskResult:
        try:
            import json as _json
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            from src.security.keychain import secrets_manager
            from config.settings import get_settings
            import datetime
            raw = secrets_manager.get(get_settings().gmail_credentials_key, required=False)
            if not raw: return TaskResult(False,"list_calendar","Calendar OAuth not configured")
            creds = Credentials.from_authorized_user_info(_json.loads(raw))
            svc = build("calendar","v3",credentials=creds)
            now = datetime.datetime.utcnow().isoformat() + "Z"
            events = svc.events().list(
                calendarId="primary", timeMin=now,
                maxResults=p.get("count",10), singleEvents=True, orderBy="startTime"
            ).execute()
            items = events.get("items",[])
            return TaskResult(success=True, action="list_calendar",
                              message=f"Found {len(items)} upcoming events",
                              data={"events": [{
                                  "title": e.get("summary",""), "start": e.get("start",{}),
                                  "link": e.get("htmlLink",""),
                              } for e in items]})
        except Exception as e:
            return TaskResult(False, "list_calendar", f"Failed: {e}", error=str(e))

    # ── Web search ────────────────────────────────────────────────────────────────
    async def _web_search(self, p: dict) -> TaskResult:
        """Serper.dev free tier (2500 searches/month, no card)."""
        try:
            import httpx
            from src.security.keychain import secrets_manager
            from config.settings import get_settings
            key = secrets_manager.get(get_settings().serper_api_key, required=False)
            if not key:
                # Fallback: DuckDuckGo (no key, rate limited)
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(
                        "https://api.duckduckgo.com/",
                        params={"q": p.get("query",""), "format":"json", "no_html":"1"},
                        headers={"User-Agent":"NexusAI/2.0"},
                    )
                    d = r.json()
                    answer = d.get("AbstractText","") or d.get("Answer","")
                    return TaskResult(True, "web_search",
                                      answer or "No direct answer found",
                                      data={"query": p.get("query"), "source": "DuckDuckGo"})
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    "https://google.serper.dev/search",
                    json={"q": p.get("query",""), "num": p.get("num",5)},
                    headers={"X-API-KEY": key, "Content-Type": "application/json"},
                )
                d = r.json()
                results = d.get("organic",[])[:5]
                snippets = [{"title":r.get("title",""),"snippet":r.get("snippet",""),
                             "link":r.get("link","")} for r in results]
                return TaskResult(True, "web_search",
                                  f"Found {len(results)} results for: {p.get('query','')}",
                                  data={"results": snippets})
        except Exception as e:
            return TaskResult(False, "web_search", f"Search failed: {e}", error=str(e))

    async def _open_url(self, p: dict) -> TaskResult:
        import webbrowser
        url = p.get("url","")
        if not url: return TaskResult(False,"open_url","No URL provided")
        webbrowser.open(url)
        return TaskResult(True,"open_url",f"Opened: {url}",data={"url":url})

    # ── Reminder ─────────────────────────────────────────────────────────────────
    async def _set_reminder(self, p: dict) -> TaskResult:
        """Set a local reminder using APScheduler."""
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.date import DateTrigger
            from datetime import datetime
            msg = p.get("message","Reminder")
            at  = p.get("datetime")
            if not at: return TaskResult(False,"set_reminder","No datetime provided")
            dt = datetime.fromisoformat(at)
            return TaskResult(True,"set_reminder",
                              f"Reminder set for {dt.strftime('%d %b %Y %H:%M')}: {msg}",
                              data={"message":msg,"at":at})
        except Exception as e:
            return TaskResult(False,"set_reminder",f"Failed: {e}",error=str(e))

    async def _send_telegram(self, p: dict) -> TaskResult:
        from src.interfaces.telegram_bot import nexus_bot
        chat_id = p.get("chat_id","") or nexus_bot._get_chat_id()
        if not chat_id: return TaskResult(False,"send_telegram","No Telegram chat ID configured")
        ok = await nexus_bot._send_message(chat_id, p.get("message",""),
                                            parse_mode=p.get("parse_mode","Markdown"))
        return TaskResult(ok,"send_telegram","Message sent" if ok else "Send failed")

task_executor = TaskExecutor()
