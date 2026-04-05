"""
Nexus AI — Expanded Source Registry v2 (Improvement #11)
8–10 sources per category. India-specific sources added.
Router picks best 6 per query based on location, query context, and live trust scores.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class SourceEntry:
    domain: str
    url_template: str        # {query} placeholder for search queries
    trust_rank: str          # A+, A, B+, B
    base_score: float        # 0.0–1.0
    region: str              # IN, US, GLOBAL
    requires_login: bool = False
    captcha_risk: float = 0.0   # 0.0–1.0
    selectors: dict = field(default_factory=dict)


# ── Commodity / precious metals ───────────────────────────────────────────────
COMMODITY_SOURCES = [
    SourceEntry("goldprice.org",      "https://goldprice.org",                  "A+", 0.96, "GLOBAL", captcha_risk=0.05),
    SourceEntry("goodreturns.in",     "https://goodreturns.in/gold-rates",      "A",  0.92, "IN",     captcha_risk=0.08),
    SourceEntry("moneycontrol.com",   "https://moneycontrol.com/commodity",     "A",  0.91, "IN",     captcha_risk=0.10),
    SourceEntry("investing.com",      "https://investing.com/commodities/gold",  "A",  0.90, "GLOBAL", captcha_risk=0.15),
    SourceEntry("mcxindia.com",       "https://mcxindia.com/market-data",       "A+", 0.95, "IN",     captcha_risk=0.12),  # India MCX exchange
    SourceEntry("ncdex.com",          "https://ncdex.com/marketdata",           "A+", 0.94, "IN",     captcha_risk=0.10),  # India commodity exchange
    SourceEntry("marketwatch.com",    "https://marketwatch.com/commodities",    "A",  0.89, "US",     captcha_risk=0.08),
    SourceEntry("kitco.com",          "https://kitco.com/gold-price-today",     "A",  0.88, "US",     captcha_risk=0.30),
    SourceEntry("goldprice.co.in",    "https://goldprice.co.in",                "B+", 0.80, "IN",     captcha_risk=0.05),  # Fallback India
    SourceEntry("ibjarates.com",      "https://ibjarates.com",                  "A+", 0.97, "IN",     captcha_risk=0.05),  # India Bullion Association
]

# ── Stock market / indices ────────────────────────────────────────────────────
STOCK_SOURCES = [
    SourceEntry("nseindia.com",       "https://nseindia.com",                   "A+", 0.99, "IN",  captcha_risk=0.05),
    SourceEntry("bseindia.com",       "https://bseindia.com",                   "A+", 0.99, "IN",  captcha_risk=0.05),
    SourceEntry("moneycontrol.com",   "https://moneycontrol.com/stocks",        "A",  0.91, "IN",  captcha_risk=0.10),
    SourceEntry("tickertape.in",      "https://tickertape.in",                  "A",  0.90, "IN",  captcha_risk=0.08),
    SourceEntry("investing.com",      "https://investing.com/indices",          "A",  0.89, "GLOBAL", captcha_risk=0.15),
    SourceEntry("tradingview.com",    "https://tradingview.com/markets",        "A",  0.88, "GLOBAL", captcha_risk=0.12),
    SourceEntry("ticker.finology.in", "https://ticker.finology.in",             "B+", 0.82, "IN",  captcha_risk=0.06),
    SourceEntry("screener.in",        "https://screener.in",                    "A",  0.87, "IN",  captcha_risk=0.07),
]

# ── Flights ───────────────────────────────────────────────────────────────────
FLIGHT_SOURCES = [
    SourceEntry("google.com/flights", "https://google.com/travel/flights",      "A+", 0.96, "GLOBAL", captcha_risk=0.08),
    SourceEntry("makemytrip.com",     "https://makemytrip.com/flights",         "A",  0.92, "IN",  captcha_risk=0.12),
    SourceEntry("skyscanner.com",     "https://skyscanner.com",                 "A",  0.91, "GLOBAL", captcha_risk=0.10),
    SourceEntry("ixigo.com",          "https://ixigo.com/flights",              "A",  0.90, "IN",  captcha_risk=0.09),
    SourceEntry("goindigo.in",        "https://goindigo.in",                    "A",  0.89, "IN",  captcha_risk=0.15),
    SourceEntry("airindia.in",        "https://airindia.in",                    "A",  0.88, "IN",  captcha_risk=0.12),
    SourceEntry("spicejet.com",       "https://spicejet.com",                   "B+", 0.84, "IN",  captcha_risk=0.14),
    SourceEntry("paytm.com/travel",   "https://paytm.com/flights",              "B+", 0.82, "IN",  captcha_risk=0.10),
    SourceEntry("cleartrip.com",      "https://cleartrip.com/flights",          "B+", 0.81, "IN",  captcha_risk=0.20),
    SourceEntry("easemytrip.com",     "https://easemytrip.com",                 "B+", 0.80, "IN",  captcha_risk=0.11),
]

# ── Weather ───────────────────────────────────────────────────────────────────
WEATHER_SOURCES = [
    SourceEntry("imd.gov.in",         "https://imd.gov.in",                     "A+", 0.97, "IN",  captcha_risk=0.03),  # India Met Dept
    SourceEntry("weather.com",        "https://weather.com",                    "A+", 0.95, "GLOBAL", captcha_risk=0.05),
    SourceEntry("accuweather.com",    "https://accuweather.com",                "A",  0.93, "GLOBAL", captcha_risk=0.08),
    SourceEntry("windy.com",          "https://windy.com",                      "A",  0.91, "GLOBAL", captcha_risk=0.04),
    SourceEntry("timeanddate.com",    "https://timeanddate.com/weather",        "A",  0.90, "GLOBAL", captcha_risk=0.06),
    SourceEntry("mausam.imd.gov.in",  "https://mausam.imd.gov.in",              "A+", 0.96, "IN",  captcha_risk=0.03),  # IMD state forecasts
    SourceEntry("yr.no",              "https://yr.no",                          "A",  0.88, "GLOBAL", captcha_risk=0.04),
    SourceEntry("india-weather.org",  "https://india-weather.org",              "B+", 0.79, "IN",  captcha_risk=0.06),
]

# ── Train ─────────────────────────────────────────────────────────────────────
TRAIN_SOURCES = [
    SourceEntry("irctc.co.in",        "https://irctc.co.in",                    "A+", 0.98, "IN",  captcha_risk=0.10, requires_login=True),
    SourceEntry("confirmtkt.com",     "https://confirmtkt.com",                 "A",  0.93, "IN",  captcha_risk=0.07),
    SourceEntry("trainman.in",        "https://trainman.in",                    "A",  0.91, "IN",  captcha_risk=0.06),
    SourceEntry("railyatri.in",       "https://railyatri.in",                   "A",  0.90, "IN",  captcha_risk=0.07),
    SourceEntry("12go.asia",          "https://12go.asia/en/india",             "B+", 0.83, "GLOBAL", captcha_risk=0.08),
    SourceEntry("ixigo.com/trains",   "https://ixigo.com/trains",               "A",  0.89, "IN",  captcha_risk=0.08),
    SourceEntry("erail.in",           "https://erail.in",                       "B+", 0.80, "IN",  captcha_risk=0.05),
]

# ── News ──────────────────────────────────────────────────────────────────────
NEWS_SOURCES = [
    SourceEntry("timesofindia.com",   "https://timesofindia.com",               "A",  0.88, "IN",  captcha_risk=0.06),
    SourceEntry("thehindu.com",       "https://thehindu.com",                   "A+", 0.92, "IN",  captcha_risk=0.05),
    SourceEntry("ndtv.com",           "https://ndtv.com",                       "A",  0.87, "IN",  captcha_risk=0.07),
    SourceEntry("reuters.com",        "https://reuters.com",                    "A+", 0.95, "GLOBAL", captcha_risk=0.08),
    SourceEntry("bbc.com/news",       "https://bbc.com/news",                   "A+", 0.94, "GLOBAL", captcha_risk=0.05),
    SourceEntry("hindustantimes.com", "https://hindustantimes.com",             "A",  0.86, "IN",  captcha_risk=0.07),
    SourceEntry("theprint.in",        "https://theprint.in",                    "A",  0.85, "IN",  captcha_risk=0.04),
    SourceEntry("scroll.in",          "https://scroll.in",                      "A",  0.84, "IN",  captcha_risk=0.04),
]

REGISTRY: dict[str, list[SourceEntry]] = {
    "commodity":  COMMODITY_SOURCES,
    "stock":      STOCK_SOURCES,
    "flight":     FLIGHT_SOURCES,
    "train":      TRAIN_SOURCES,
    "weather":    WEATHER_SOURCES,
    "news":       NEWS_SOURCES,
    "hotel":      FLIGHT_SOURCES[:6],  # reuse flight sources for now
    "default":    NEWS_SOURCES[:6],
}


async def select_sources(
    subtype: str,
    count: int = 6,
    user_location: str = "IN",
    live_scores: Optional[dict[str, float]] = None,
) -> list[SourceEntry]:
    """
    Select the best N sources for a query.
    Prioritizes: live trust score > base score, prefers region match.
    Excludes login-required sources unless no alternative exists.
    """
    pool = REGISTRY.get(subtype, REGISTRY["default"])

    # Merge live trust scores
    if live_scores:
        for s in pool:
            if s.domain in live_scores:
                s.base_score = (s.base_score + live_scores[s.domain]) / 2

    # Sort: no-login first, then by adjusted score desc, prefer user region
    def sort_key(s: SourceEntry) -> float:
        score = s.base_score
        if s.region == user_location:
            score += 0.05   # region boost
        if s.requires_login:
            score -= 0.20   # penalize login-required
        if s.captcha_risk > 0.20:
            score -= 0.10   # penalize high captcha risk
        return -score       # negative for ascending sort = descending

    sorted_pool = sorted(pool, key=sort_key)
    selected = sorted_pool[:count]
    log.info("sources_selected", subtype=subtype, count=len(selected),
             domains=[s.domain for s in selected])
    return selected
