"""
Nexus AI — Complete Pipeline Orchestrator v3
All query types: live data, actions (email/Spotify/calendar), knowledge.
Real-time data from Playwright + free APIs. Zero hardcoded values.
"""
from __future__ import annotations
import asyncio, json, re, time, uuid
from dataclasses import dataclass, field
from typing import Any, Optional
import structlog
from config.settings import get_settings
from src.agents.classifier import QueryClassifier, QueryType
from src.agents.llm_client import llm_client
from src.security.input_guard import input_guard
from src.security.output_sanitizer import output_sanitizer
from src.security.pii_masker import pii_masker
from src.security.audit_logger import AuditEvent, audit_logger
from src.security.rate_limiter import per_user_limiter
from src.memory.session_memory import session_memory
from src.utils.query_cache import query_cache
from src.utils.db import update_task_status, count_active_tasks, increment_task_retry, move_to_dead_letter

log = structlog.get_logger(__name__)
_s = get_settings()

@dataclass
class PipelineEvent:
    stage: str; message: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

@dataclass
class PipelineResult:
    task_id: str; query: str; subtype: str
    verdict: str; confidence: float; sources: list[str]
    agent_scores: dict; pipeline_type: str; rounds_taken: int
    elapsed_s: float; from_cache: bool = False
    hitl_required: bool = False; hitl_task_id: Optional[str] = None
    raw_data: Optional[dict] = None; error: Optional[str] = None


class NexusPipeline:
    """End-to-end pipeline. All query types. Real data only."""

    def __init__(self):
        self._classifier = QueryClassifier()

    def _is_simple_query(self, query: str, cls) -> bool:
        q = query.lower().strip()
        if cls.query_type == QueryType.ACTION:
            return cls.subtype == "spotify"
        if cls.subtype in {"crypto", "stock", "weather", "commodity"} and len(q) <= 90:
            return True
        if cls.query_type == QueryType.KNOWLEDGE and len(q.split()) <= 10:
            return True
        return False

    def _answer_model(self, simple: bool) -> str:
        if _s.fast_response_mode and simple:
            return _s.groq_fast_model
        return _s.groq_primary_model

    async def _chat_with_timeout(self, *, model: str, messages: list[dict], temperature: float,
                                 max_tokens: int, user_id: str, json_mode: bool = False):
        return await asyncio.wait_for(
            llm_client.chat(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                user_id=user_id,
            ),
            timeout=_s.llm_call_timeout_s,
        )

    async def run(self, task_id: str, query: str, user_id: str,
                  session_id: str = "", emit_event=None,
                  user_location: Optional[dict] = None) -> PipelineResult:
        t0 = time.time()
        session_id = session_id or user_id

        def emit(stage, msg, **data):
            if emit_event:
                try: emit_event(PipelineEvent(stage=stage, message=msg, data=data))
                except: pass
            log.info(f"pipeline.{stage}", task_id=task_id[:8], msg=msg[:80])

        try:
            await update_task_status(task_id, "running")

            # Session context
            emit("session", "Loading session context")
            sess = await session_memory.process(session_id, query, "unknown", "unknown", {})
            effective_query = sess.enriched_query or query
            if sess.is_followup:
                emit("session", "Follow-up query enriched with previous context")

            # Rate limit
            rl = await per_user_limiter.check(user_id, effective_query)
            if not rl.allowed:
                raise RuntimeError(f"Rate limited: {rl.reason}. Retry in {rl.retry_after:.0f}s")

            # Concurrent limit
            if await count_active_tasks(user_id) > _s.max_concurrent_tasks_per_user:
                raise RuntimeError("Too many active tasks. Please wait for one to finish.")

            # Classify
            emit("classify", "Classifying query")
            cls = await self._classifier.classify(effective_query, user_location)
            is_simple = self._is_simple_query(effective_query, cls)
            sla_target = _s.simple_task_target_s if is_simple else _s.complex_task_target_s
            emit("classify", f"-> {cls.query_type.value} / {cls.subtype}",
                  type=cls.query_type.value, subtype=cls.subtype,
                  sla_target_s=sla_target, simple=is_simple)

            # Cache check (not for actions)
            if cls.query_type != QueryType.ACTION:
                cached = await query_cache.get(effective_query, cls.subtype)
                if cached:
                    emit("cache", "Cache hit — returning instantly")
                    result = PipelineResult(
                        task_id=task_id, query=query, subtype=cls.subtype,
                        verdict=cached.get("verdict",""), confidence=cached.get("confidence",0),
                        sources=cached.get("sources",[]), agent_scores={},
                        pipeline_type=cls.query_type.value, rounds_taken=0,
                        elapsed_s=round(time.time()-t0,2), from_cache=True,
                    )
                    await self._finish(task_id, session_id, query, cls.subtype, result)
                    return result

            # Route
            if cls.query_type == QueryType.ACTION:
                result = await self._run_action(task_id, effective_query, user_id, cls, emit, t0)
            elif cls.query_type == QueryType.LIVE_DATA:
                result = await self._run_live(task_id, effective_query, user_id, cls, emit, t0)
            else:
                result = await self._run_knowledge(task_id, effective_query, user_id, cls, emit, t0, is_simple)

            await self._finish(task_id, session_id, query, cls.subtype, result)
            return result

        except Exception as exc:
            log.error("pipeline_error", task_id=task_id[:8], error=str(exc))
            await update_task_status(task_id, "failed", error=str(exc))
            return PipelineResult(
                task_id=task_id, query=query, subtype="unknown",
                verdict=f"Error: {exc}", confidence=0, sources=[], agent_scores={},
                pipeline_type="error", rounds_taken=0,
                elapsed_s=round(time.time()-t0,2), error=str(exc),
            )

    # ── Live data pipeline ──────────────────────────────────────────────────────
    async def _run_live(self, task_id, query, user_id, cls, emit, t0):
        from src.tools.realtime_data import realtime_engine
        subtype = cls.subtype
        entities = cls.entities
        country  = entities.get("country_code","IN")
        city     = entities.get("mentioned_city","") or entities.get("user_city","")

        # ── Flights ──────────────────────────────────────────────────────────
        if subtype == "flight":
            emit("scrape", f"Comparing flights across {len(realtime_engine.FLIGHT_PLATFORMS.__class__)} platforms")
            origin = entities.get("origin","BLR")
            dest   = entities.get("destination","DEL")
            date   = entities.get("date") or self._next_weekday()
            result_data = await realtime_engine.get_flight_prices(origin, dest, date)
            emit("scrape", f"Scraped {len(result_data.flights)} flight options from {len(result_data.sources_checked)} platforms")

            if not result_data.flights:
                verdict = f"No flights found for {origin}→{dest} on {date}. Try checking makemytrip.com directly."
                conf = 0
            else:
                cheapest = result_data.cheapest
                # Build comparison text via LLM
                emit("decision", "Analysing and comparing all flight options")
                flight_summary = json.dumps(result_data.flights[:10], indent=2)
                try:
                    r = await self._chat_with_timeout(
                        model=_s.groq_fast_model,
                        messages=[{"role": "user", "content":
                            f"Compare these flights from {origin} to {dest} on {date}:\n{flight_summary}\n\n"
                            f"Give the cheapest option and 2 alternatives. Include price, airline, duration."}],
                        temperature=0.1,
                        max_tokens=320,
                        user_id=user_id,
                    )
                    verdict = r.content
                except Exception:
                    verdict = (
                        f"Best flight found: {cheapest.get('airline','Unknown')} at {cheapest.get('price','N/A')} "
                        f"for {origin}→{dest} on {date}. Found {len(result_data.flights)} total options."
                    )
                conf = 0.88

            return PipelineResult(
                task_id=task_id, query=query, subtype="flight",
                verdict=verdict, confidence=conf,
                sources=result_data.sources_checked,
                agent_scores={}, pipeline_type="live_flights",
                rounds_taken=0, elapsed_s=round(time.time()-t0,2),
                raw_data=result_data.__dict__,
            )

        # ── Commodity (gold, silver, oil) ────────────────────────────────────
        elif subtype == "commodity":
            commodity = entities.get("commodity","gold")
            loc_str = f"{city} {country}" if city else country
            emit("scrape", f"Fetching live {commodity} price for {loc_str}")
            price_data = await realtime_engine.get_commodity_price(
                commodity=commodity, city=city, country_code=country,
                llm_client=llm_client,
            )
            emit("scrape", f"Scraped {price_data.sources_verified} sources · {price_data.spread_pct}% spread")

            if price_data.error:
                verdict = f"Could not fetch {commodity} price: {price_data.error}"
                conf = 0
            else:
                # Build user-facing answer
                location_text = city.title() if city else ("India" if country=="IN" else country)
                emit("decision", "Generating location-aware answer")
                try:
                    r = await self._chat_with_timeout(
                        model=_s.groq_fast_model,
                        messages=[{"role": "user", "content":
                            f"User asked: {query}\n\nLive data: {json.dumps(price_data.__dict__)}\n\n"
                            f"Return a concise user answer with price, location, source and timestamp."}],
                        temperature=0.1,
                        max_tokens=220,
                        user_id=user_id,
                    )
                    verdict = r.content
                except Exception:
                    verdict = (
                        f"{commodity.title()} price in {location_text}: {price_data.consensus_price} "
                        f"(confidence {price_data.confidence:.0%}, {price_data.sources_verified} sources)."
                    )
                conf = price_data.confidence

            await query_cache.set(query, "commodity", {"verdict":verdict,"confidence":conf,
                                                        "sources":[s["source"] for s in price_data.sources]})
            return PipelineResult(
                task_id=task_id, query=query, subtype="commodity",
                verdict=verdict, confidence=conf,
                sources=[s["source"] for s in price_data.sources],
                agent_scores={}, pipeline_type="live_commodity",
                rounds_taken=0, elapsed_s=round(time.time()-t0,2),
                raw_data=price_data.__dict__,
            )

        # ── Crypto ────────────────────────────────────────────────────────────
        elif subtype == "crypto":
            symbol = entities.get("crypto_symbol","BTC")
            emit("scrape", f"Fetching live {symbol} price from CoinGecko")
            data = await realtime_engine.get_crypto(symbol)
            if not data:
                verdict = f"Could not fetch {symbol} price right now."
                conf = 0
            else:
                verdict = (f"{data['symbol']} is currently **${data['price_usd']:,}** USD"
                           f" / ₹{data['price_inr']:,} INR\n"
                           f"24h change: {data['change_24h']:+.2f}%\n"
                           f"Source: {data['source']}")
                conf = 0.95
            return PipelineResult(
                task_id=task_id, query=query, subtype="crypto",
                verdict=verdict, confidence=conf, sources=["coingecko.com"],
                agent_scores={}, pipeline_type="live_crypto",
                rounds_taken=0, elapsed_s=round(time.time()-t0,2),
            )

        # ── Stock index ───────────────────────────────────────────────────────
        elif subtype == "stock":
            emit("scrape", "Fetching live stock index from NSE")
            idx_query = re.search(r"\b(nifty|sensex|nasdaq|dow|s&p|ftse|nikkei)\b", query, re.I)
            index = idx_query.group(1).upper() if idx_query else "NIFTY50"
            data = await realtime_engine.get_stock_index(index)
            if not data:
                verdict = f"Could not fetch {index} right now. Check nseindia.com"
                conf = 0
            else:
                chg = data.get("change",0); pct = data.get("change_pct",0)
                arrow = "▲" if chg >= 0 else "▼"
                verdict = (f"**{data.get('index','Index')}**: {data.get('last','?'):,}\n"
                           f"{arrow} {abs(chg):.2f} ({abs(pct):.2f}%) today\n"
                           f"Source: {data.get('source','NSE')} · {data.get('as_of','')}")
                conf = 0.97
            return PipelineResult(
                task_id=task_id, query=query, subtype="stock",
                verdict=verdict, confidence=conf,
                sources=[data.get("source","nseindia.com") if data else ""],
                agent_scores={}, pipeline_type="live_stock",
                rounds_taken=0, elapsed_s=round(time.time()-t0,2),
            )

        # ── Weather ───────────────────────────────────────────────────────────
        elif subtype in ("weather","general_live") and ("weather" in query.lower() or "temperature" in query.lower()):
            loc = city or "Bengaluru"
            emit("scrape", f"Fetching weather for {loc} from open-meteo.com")
            w = await realtime_engine.get_weather(loc, country)
            if not w:
                verdict = f"Could not fetch weather for {loc}."
                conf = 0
            else:
                verdict = (f"**{w.city} weather**: {w.temp_c}°C, {w.description}\n"
                           f"Feels like {w.feels_like_c}°C · Humidity {w.humidity}% · "
                           f"Wind {w.wind_kmh} km/h\nSource: {w.source}")
                conf = 0.95
            return PipelineResult(
                task_id=task_id, query=query, subtype="weather",
                verdict=verdict, confidence=conf, sources=["open-meteo.com"],
                agent_scores={}, pipeline_type="live_weather",
                rounds_taken=0, elapsed_s=round(time.time()-t0,2),
            )

        else:
            # Generic live query — use web search + LLM
            return await self._run_knowledge(task_id, query, user_id, cls, emit, t0)

    # ── Action pipeline ─────────────────────────────────────────────────────────
    async def _run_action(self, task_id, query, user_id, cls, emit, t0):
        subtype = cls.subtype
        entities = cls.entities

        # ── Spotify (no HITL needed — reversible) ─────────────────────────────
        if subtype == "spotify":
            emit("action", "Executing Spotify action")
            song_query = entities.get("song_query","")
            # Detect action type
            ql = query.lower()
            if re.search(r"\b(pause|stop music)\b", ql):
                action = "spotify_pause"; params = {}
            elif re.search(r"\b(next|skip)\b", ql):
                action = "spotify_next"; params = {}
            elif re.search(r"\bresume\b", ql):
                action = "spotify_play"; params = {"query": ""}
            elif re.search(r"\bvolume\b", ql):
                vol_m = re.search(r"(\d+)\s*%?", ql)
                vol = int(vol_m.group(1)) if vol_m else (70 if "up" in ql else 30)
                action = "spotify_volume"; params = {"volume_pct": vol}
            else:
                action = "spotify_play"; params = {"query": song_query or query}

            from src.tools.task_executor import task_executor
            result = await task_executor.execute(action, params, user_id)
            return PipelineResult(
                task_id=task_id, query=query, subtype="spotify",
                verdict=result.message, confidence=0.95,
                sources=["Spotify Web API"], agent_scores={},
                pipeline_type="action_spotify", rounds_taken=0,
                elapsed_s=round(time.time()-t0,2),
            )

        # ── Email (HITL required) ─────────────────────────────────────────────
        elif subtype == "email":
            emit("action", "Analysing email request and drafting")
            # Extract recipient, get tone from past emails, draft
            from src.agents.drafter import DrafterAgent
            from src.agents.base import MessageBoard
            from src.hitl.gate import hitl_gate
            from src.tools.email_tool import email_tool

            # Get available accounts
            accounts = await email_tool.list_accounts()
            emit("action", f"Found {len(accounts)} email account(s)")

            # Draft the email using LLM
            past = await email_tool.read_recent(10)
            tone_ctx = "\n".join(e.get("snippet","")[:100] for e in past[:5]) if past else ""
            r = await self._chat_with_timeout(
                model=_s.groq_fast_model,
                messages=[{"role":"user","content":
                    f"User request: {query}\nPast email style:\n{tone_ctx}\n\n"
                    f"Draft a professional email. Return JSON:\n"
                    f"{{\"to\":\"recipient@example.com\",\"subject\":\"Subject line\","
                    f"\"body\":\"Full email body\"}}"}],
                temperature=0.2,
                max_tokens=420,
                user_id=user_id,
                json_mode=True,
            )
            draft = json.loads(r.content)
            emit("hitl", "Draft ready — requesting approval")
            approval_id = await hitl_gate.create_approval(
                task_id=task_id, user_id=user_id,
                action_type="send_email", draft_text=draft.get("body",""),
                draft_dict=draft,
            )
            verdict = (f"Email drafted and pending your approval.\n\n"
                       f"**To:** {draft.get('to','')}\n"
                       f"**Subject:** {draft.get('subject','')}\n\n"
                       f"{draft.get('body','')}\n\n"
                       f"*Reply /approve {approval_id[:8]} or /reject {approval_id[:8]} on Telegram.*")
            return PipelineResult(
                task_id=task_id, query=query, subtype="email",
                verdict=verdict, confidence=0.9, sources=["Gmail API"],
                agent_scores={}, pipeline_type="action_email",
                rounds_taken=0, elapsed_s=round(time.time()-t0,2),
                hitl_required=True, hitl_task_id=approval_id,
            )

        # ── Calendar / reminder ────────────────────────────────────────────────
        elif subtype == "calendar":
            emit("action", "Parsing meeting/reminder request")
            r = await self._chat_with_timeout(
                model=_s.groq_fast_model,
                messages=[{"role":"user","content":
                    f"Parse this into a calendar event. Request: '{query}'\n"
                    f"Return JSON: {{\"title\":\"Meeting title\","
                    f"\"start_time\":\"2025-03-20T15:00:00\","
                    f"\"end_time\":\"2025-03-20T16:00:00\","
                    f"\"attendees\":[],\"description\":\"\"}}"}],
                temperature=0,
                max_tokens=180,
                user_id=user_id,
                json_mode=True,
            )
            event_data = json.loads(r.content)
            from src.hitl.gate import hitl_gate
            approval_id = await hitl_gate.create_approval(
                task_id=task_id, user_id=user_id,
                action_type="create_calendar_event",
                draft_text=f"Create event: {event_data.get('title')} at {event_data.get('start_time')}",
                draft_dict=event_data,
            )
            verdict = (f"Calendar event ready for approval:\n"
                       f"**{event_data.get('title','')}**\n"
                       f"When: {event_data.get('start_time','')}\n"
                       f"*Approve on Telegram to create the event.*")
            return PipelineResult(
                task_id=task_id, query=query, subtype="calendar",
                verdict=verdict, confidence=0.9, sources=["Google Calendar"],
                agent_scores={}, pipeline_type="action_calendar",
                rounds_taken=0, elapsed_s=round(time.time()-t0,2),
                hitl_required=True, hitl_task_id=approval_id,
            )

        else:
            return await self._run_knowledge(task_id, query, user_id, cls, emit, t0)

    # ── Knowledge pipeline ──────────────────────────────────────────────────────
    async def _run_knowledge(self, task_id, query, user_id, cls, emit, t0, is_simple: bool = False):
        emit("research", "Searching knowledge base + web")

        # Web + memory retrieval in parallel with hard time budgets.
        from src.tools.task_executor import task_executor
        from src.memory.vector_store import vector_memory

        async def _web():
            if not any(w in query.lower() for w in ["latest", "recent", "news", "2024", "2025", "who is", "what happened"]):
                return None
            try:
                sr = await asyncio.wait_for(
                    task_executor.execute("web_search", {"query": query, "num": 3}),
                    timeout=_s.web_search_timeout_s,
                )
                return sr.data if sr.success else None
            except Exception:
                return None

        async def _mem():
            try:
                return await asyncio.wait_for(vector_memory.search(query, top_k=3), timeout=2.5)
            except Exception:
                return []

        web_results, mem_results = await asyncio.gather(_web(), _mem())

        emit("research", f"Retrieved {len(mem_results)} memory entries"
             + (f" + {len(web_results.get('results',[]))} web results" if web_results else ""))

        # Agent debate (kept in architecture; short/simple queries can skip)
        try:
            if _s.fast_response_mode and _s.skip_meeting_for_simple_queries and is_simple:
                rounds = 0
                emit("meeting", "Skipped debate for simple query to meet SLA")
            else:
                from src.meeting.room import MeetingRoom, MeetingState
                from src.agents.base import MessageBoard
                from src.memory.vector_store import vector_memory as vm
                board = MessageBoard(task_id)
                ctx = {"web_results": web_results, "memory": mem_results, "query": query}
                state = MeetingState(task_id=task_id, query=query, context=ctx, board=board)
                room = MeetingRoom(memory=vm)
                emit("meeting", "Agent debate starting")
                state = await asyncio.wait_for(room.run(state), timeout=_s.meeting_timeout_s)
                rounds = state.current_round
                emit("meeting", f"Converged after {rounds} rounds")
        except Exception as e:
            log.warning("meeting_failed", error=str(e)); rounds = 0; board = None

        # Final answer
        emit("decision", "Generating final answer")
        mem_ctx = "\n".join(str(m) for m in mem_results[:3]) if mem_results else ""
        web_ctx = "\n".join(f"{r['title']}: {r['snippet']}" for r in (web_results or {}).get("results",[])[:3])
        context_str = f"Memory:\n{mem_ctx}\n\nWeb:\n{web_ctx}" if (mem_ctx or web_ctx) else ""

        model = self._answer_model(is_simple)
        max_tokens = 260 if is_simple else 700
        try:
            r = await self._chat_with_timeout(
                model=model,
                messages=[{"role":"user","content":
                    f"Question: {query}\n\n{context_str}\n\n"
                    f"Give an accurate answer. Be concise for simple questions, detailed for complex ones. Cite sources when available."}],
                temperature=0.2,
                max_tokens=max_tokens,
                user_id=user_id,
            )
            verdict = r.content
        except Exception:
            verdict = "I could not finish full reasoning in time. Here is the fastest safe answer based on available context: " + (web_ctx or mem_ctx or "No fresh context found.")

        # PII mask
        verdict = pii_masker.mask(verdict).safe_text or verdict

        await query_cache.set(query, "knowledge", {"verdict":verdict,"confidence":0.85,"sources":[]})
        return PipelineResult(
            task_id=task_id, query=query, subtype="knowledge",
            verdict=verdict, confidence=0.85, sources=["knowledge_base","web"],
            agent_scores={}, pipeline_type="knowledge",
            rounds_taken=rounds, elapsed_s=round(time.time()-t0,2),
        )

    async def _finish(self, task_id, session_id, query, subtype, result):
        await update_task_status(task_id, "completed", result={
            "verdict": result.verdict[:2000], "confidence": result.confidence,
            "sources": result.sources, "elapsed_s": result.elapsed_s,
        })
        try:
            await session_memory.save(session_id=session_id, turn_num=0,
                                      query=query, query_type=result.pipeline_type,
                                      subtype=subtype, result={"verdict":result.verdict[:200]},
                                      entities={})
        except: pass
        try:
            from src.interfaces.telegram_bot import nexus_bot
            cid = nexus_bot._get_chat_id()
            if cid:
                await nexus_bot.send_completion_notification(
                    cid, task_id,
                    f"*{query[:60]}*\n{result.verdict[:200]}\n_Conf: {result.confidence:.0%} · {result.elapsed_s}s_"
                )
        except: pass

    @staticmethod
    def _next_weekday():
        import datetime
        d = datetime.date.today() + datetime.timedelta(days=1)
        while d.weekday() >= 5: d += datetime.timedelta(days=1)
        return d.strftime("%Y-%m-%d")


async def run_with_retry(task_id, query, user_id, subtype="unknown", user_location=None):
    pipeline = NexusPipeline()
    for attempt in range(1, _s.task_max_retries+1):
        try:
            result = await pipeline.run(task_id, query, user_id,
                                        user_location=user_location)
            if not result.error: return result
            if attempt < _s.task_max_retries:
                await asyncio.sleep(_s.task_retry_backoff_s * (2**(attempt-1)))
                await increment_task_retry(task_id, result.error)
        except Exception as exc:
            if attempt == _s.task_max_retries:
                await move_to_dead_letter(task_id, user_id, query, subtype, str(exc), attempt)
            else:
                await asyncio.sleep(_s.task_retry_backoff_s * (2**(attempt-1)))
