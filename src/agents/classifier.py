"""
Nexus AI — Query Classifier
Routes every query to the correct pipeline.
Handles: live data, actions (email/Spotify/calendar), knowledge queries.
Location-aware: extracts user city/country for price queries.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import structlog
from config.settings import get_settings
log = structlog.get_logger(__name__)
_s = get_settings()

class QueryType(str, Enum):
    LIVE_DATA  = "live_data"
    ACTION     = "action"
    KNOWLEDGE  = "knowledge"
    AMBIGUOUS  = "ambiguous"

@dataclass
class ClassificationResult:
    query_type: QueryType
    confidence: float
    entities: dict
    method: str
    pipeline: str
    subtype: str = "general"
    requires_hitl: bool = False
    notes: str = ""

# ── City / region extraction ───────────────────────────────────────────────────
INDIA_CITIES = [
    "bengaluru","bangalore","mumbai","delhi","chennai","hyderabad","pune","kolkata",
    "ahmedabad","jaipur","surat","lucknow","kanpur","nagpur","indore","bhopal",
    "patna","ludhiana","agra","nashik","vadodara","coimbatore","visakhapatnam",
    "kochi","guwahati","chandigarh","trivandrum","thiruvananthapuram","mysuru","mysore",
]
INTL_CITIES = [
    "london","new york","dubai","singapore","tokyo","sydney","toronto","paris",
    "berlin","hong kong","san francisco","los angeles","chicago","boston","seattle",
    "new delhi","kuala lumpur","bangkok","jakarta","manila","karachi","dhaka",
]
COUNTRY_MAP = {
    "india":"IN","usa":"US","us":"US","uk":"GB","uae":"AE","singapore":"SG",
    "australia":"AU","canada":"CA","germany":"DE","france":"FR","japan":"JP",
    "china":"CN","brazil":"BR","russia":"RU","south africa":"ZA","nigeria":"NG",
    "london":"GB","new york":"US","dubai":"AE","sydney":"AU","toronto":"CA",
    "paris":"FR","berlin":"DE","tokyo":"JP","hong kong":"HK","singapore":"SG",
}

# ── Keyword patterns ───────────────────────────────────────────────────────────
_LIVE = [
    r"\b(?:price|rate|cost|today|right now|live|current|latest|now)\b",
    r"\b(?:gold|silver|platinum|oil|crude|petrol|diesel|natural gas)\b",
    r"\b(?:stock|share|nifty|sensex|nasdaq|dow|s&p|sp500|ftse|nikkei)\b",
    r"\b(?:bitcoin|btc|ethereum|eth|crypto|coin)\b",
    r"\b(?:flight|flights|fly|airline|airfare|ticket)\b",
    r"\b(?:hotel|accommodation|stay)\b",
    r"\b(?:weather|temperature|forecast|rain|humidity)\b",
    r"\b(?:forex|exchange rate|usd|inr|eur|gbp|currency)\b",
    r"\b(?:train|irctc|bus|ticket availability)\b",
    r"\b(?:fuel|petrol|diesel) price\b",
]
_ACTION = [
    r"\b(?:play|pause|resume|skip|next song|previous song|stop music)\b",
    r"\bplay\b.{0,30}\b(?:song|music|playlist|album|artist|track)\b",
    r"\b(?:send|write|draft|compose)\b.{0,20}\b(?:email|mail|message)\b",
    r"\b(?:email|mail)\b.{0,20}\bto\b",
    r"\b(?:book|schedule|set up|create|add)\b.{0,20}\b(?:meeting|appointment|call|event)\b",
    r"\b(?:post|share|tweet|publish)\b.{0,20}\b(?:slack|twitter|linkedin|instagram)\b",
    r"\b(?:set|create|add)\b.{0,20}\b(?:reminder|alarm|notification)\b",
    r"\b(?:open|launch|start)\b.{0,20}\b(?:spotify|chrome|app|browser)\b",
    r"\b(?:search|find|look up)\b.{0,20}\b(?:on spotify|in spotify)\b",
    r"\bvolume\b.{0,20}\b(?:up|down|\d+%|\d+ percent)\b",
    r"\bturn (?:up|down|off) the (?:music|volume|sound)\b",
]
_KNOWLEDGE = [
    r"\bexplain\b",r"\bwhat is\b",r"\bhow does\b",r"\bwhy is\b",r"\bwhy does\b",
    r"\bwhat are\b",r"\bdefine\b",r"\bdescribe\b",r"\btell me about\b",
    r"\bhistory of\b",r"\bdifference between\b",r"\bcompare\b",r"\bsummariz[e|s]\b",
    r"\btranslate\b",r"\bin (?:hindi|kannada|tamil|telugu|malayalam|bengali|marathi)\b",
    r"\bmeaning of\b",r"\bhow to\b",r"\bcan you (?:explain|tell|describe)\b",
]

# ── Action subtypes ────────────────────────────────────────────────────────────
_SPOTIFY_PAT = re.compile(
    r"\b(?:play|pause|skip|next|previous|resume|search|find)\b.{0,30}\b(?:song|music|spotify|track|playlist|album|artist)\b"
    r"|\bplay\b.{0,50}"
    r"|\b(?:pause|resume|skip|next song|stop music)\b"
    r"|\bvolume\b",
    re.I
)
_EMAIL_PAT = re.compile(
    r"\b(?:send|write|draft|compose|email|mail)\b.{0,30}\b(?:to|email|mail|message)\b"
    r"|\b(?:email|mail)\b.{0,20}\bto\b",
    re.I
)
_CALENDAR_PAT = re.compile(
    r"\b(?:book|schedule|create|add|set)\b.{0,20}\b(?:meeting|appointment|call|event|reminder)\b"
    r"|\bremind me\b",
    re.I
)
_FLIGHT_PAT = re.compile(
    r"\b(?:flight|fly|airline|airfare)\b.{0,30}\b(?:from|to|between)\b"
    r"|\b(?:cheapest|best|compare)\b.{0,20}\b(?:flight|flights)\b",
    re.I
)
_COMMODITY_PAT = re.compile(
    r"\b(?:gold|silver|platinum|palladium|oil|crude|petrol|diesel|natural gas)\b.{0,20}\b(?:price|rate|today|cost|per)\b"
    r"|\bprice of\b.{0,20}\b(?:gold|silver|oil)\b",
    re.I
)
_CRYPTO_PAT = re.compile(
    r"\b(?:bitcoin|btc|ethereum|eth|solana|sol|crypto|coin|token)\b.{0,20}\b(?:price|rate|today|usd|inr)\b",
    re.I
)
_STOCK_PAT = re.compile(
    r"\b(?:nifty|sensex|nasdaq|dow|s&p|sp500|ftse|nikkei|stock market|share market|index)\b",
    re.I
)


class QueryClassifier:

    async def classify(self, query: str, user_location: Optional[dict] = None) -> ClassificationResult:
        ql = query.lower().strip()
        entities = self._extract_entities(ql, user_location)

        # ── 1. Fast keyword path ───────────────────────────────────────────────
        # Action: Spotify
        if _SPOTIFY_PAT.search(ql):
            song_m = re.search(r"play\s+(.+?)(?:\s+(?:on spotify|by|from|playlist|album))?$", ql, re.I)
            return ClassificationResult(
                query_type=QueryType.ACTION, confidence=0.96,
                entities={**entities, "song_query": song_m.group(1).strip() if song_m else ql},
                method="keyword", pipeline="task_executor → spotify_tool",
                subtype="spotify", requires_hitl=False,
            )

        # Action: Email
        if _EMAIL_PAT.search(ql):
            return ClassificationResult(
                query_type=QueryType.ACTION, confidence=0.95,
                entities=entities, method="keyword",
                pipeline="drafter → HITL → email_tool",
                subtype="email", requires_hitl=True,
            )

        # Action: Calendar / reminder
        if _CALENDAR_PAT.search(ql):
            return ClassificationResult(
                query_type=QueryType.ACTION, confidence=0.93,
                entities=entities, method="keyword",
                pipeline="orchestrator → HITL → calendar_tool",
                subtype="calendar", requires_hitl=True,
            )

        # Live: Flights
        if _FLIGHT_PAT.search(ql):
            return ClassificationResult(
                query_type=QueryType.LIVE_DATA, confidence=0.97,
                entities=entities, method="keyword",
                pipeline="6× browser → flight_comparator → decision",
                subtype="flight", requires_hitl=False,
            )

        # Live: Commodity
        if _COMMODITY_PAT.search(ql):
            return ClassificationResult(
                query_type=QueryType.LIVE_DATA, confidence=0.98,
                entities=entities, method="keyword",
                pipeline="realtime_engine → browser → verify → meeting → decision",
                subtype="commodity", requires_hitl=False,
            )

        # Live: Crypto
        if _CRYPTO_PAT.search(ql):
            return ClassificationResult(
                query_type=QueryType.LIVE_DATA, confidence=0.97,
                entities=entities, method="keyword",
                pipeline="coingecko_api → verify → decision",
                subtype="crypto", requires_hitl=False,
            )

        # Live: Stocks
        if _STOCK_PAT.search(ql):
            return ClassificationResult(
                query_type=QueryType.LIVE_DATA, confidence=0.97,
                entities=entities, method="keyword",
                pipeline="nse_api → browser → verify → decision",
                subtype="stock", requires_hitl=False,
            )

        # Live: General
        if any(re.search(p, ql) for p in _LIVE):
            return ClassificationResult(
                query_type=QueryType.LIVE_DATA, confidence=0.87,
                entities=entities, method="keyword",
                pipeline="realtime_engine → verify → decision",
                subtype="general_live", requires_hitl=False,
            )

        # Knowledge
        if any(re.search(p, ql) for p in _KNOWLEDGE):
            return ClassificationResult(
                query_type=QueryType.KNOWLEDGE, confidence=0.85,
                entities=entities, method="keyword",
                pipeline="researcher → debate → synthesizer",
                subtype="knowledge", requires_hitl=False,
            )

        # ── 2. LLM fallback for ambiguous queries ──────────────────────────────
        try:
            return await self._llm_classify(query, entities)
        except Exception as e:
            log.warning("llm_classify_failed", error=str(e))
            return ClassificationResult(
                query_type=QueryType.KNOWLEDGE, confidence=0.5,
                entities=entities, method="fallback",
                pipeline="researcher → decision", subtype="general",
            )

    def _extract_entities(self, query: str, user_location: Optional[dict] = None) -> dict:
        ents: dict = {}
        # User's location as default
        if user_location:
            ents["user_city"] = user_location.get("city","")
            ents["user_country"] = user_location.get("country_code","IN")

        # Extract mentioned city
        for city in INDIA_CITIES + INTL_CITIES:
            if re.search(rf"\b{re.escape(city)}\b", query, re.I):
                ents["mentioned_city"] = city
                ents["country_code"] = COUNTRY_MAP.get(city, ents.get("user_country","IN"))
                break

        # Extract country
        for country, code in COUNTRY_MAP.items():
            if re.search(rf"\b{re.escape(country)}\b", query, re.I):
                ents["country_code"] = code
                break

        # Extract IATA codes for flights
        iata = re.findall(r"\b([A-Z]{3})\b", query.upper())
        if len(iata) >= 2:
            ents["origin"] = iata[0]
            ents["destination"] = iata[1]

        # Extract date
        date_m = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b", query)
        if date_m: ents["date"] = date_m.group(1)

        # Extract commodity
        for c in ["gold","silver","platinum","oil","crude","petrol","diesel"]:
            if re.search(rf"\b{c}\b", query, re.I):
                ents["commodity"] = c; break

        # Extract crypto
        for sym in ["BTC","ETH","SOL","BNB","bitcoin","ethereum"]:
            if re.search(rf"\b{re.escape(sym)}\b", query, re.I):
                ents["crypto_symbol"] = sym.upper()[:3]; break

        # Default country from city
        if not ents.get("country_code"):
            ents["country_code"] = ents.get("user_country","IN")

        return ents

    async def _llm_classify(self, query: str, entities: dict) -> ClassificationResult:
        from src.agents.llm_client import llm_client
        r = await llm_client.chat(
            model=_s.groq_fast_model,
            messages=[{"role":"user","content":
                f"Classify this query. Query: '{query}'\n"
                f"Return ONLY JSON: {{\"type\":\"live_data|action|knowledge\","
                f"\"subtype\":\"commodity|flight|stock|weather|email|spotify|calendar|general\","
                f"\"confidence\":0.9,\"requires_hitl\":false}}"}],
            temperature=0, max_tokens=80, json_mode=True, cache_ttl=3600,
        )
        import json
        d = json.loads(r.content)
        type_map = {"live_data":QueryType.LIVE_DATA,"action":QueryType.ACTION,"knowledge":QueryType.KNOWLEDGE}
        qt = type_map.get(d.get("type","knowledge"), QueryType.KNOWLEDGE)
        return ClassificationResult(
            query_type=qt, confidence=float(d.get("confidence",0.7)),
            entities=entities, method="llm",
            pipeline="llm-classified", subtype=d.get("subtype","general"),
            requires_hitl=d.get("requires_hitl",False),
        )

QueryClassifier = QueryClassifier
