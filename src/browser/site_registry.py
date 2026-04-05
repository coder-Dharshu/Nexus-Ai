"""
Nexus AI — Site Registry.

Maps query categories to ordered lists of trusted sources.
Trust ranks: A=1.00, A-=0.92, B+=0.85, B=0.70
All domains are whitelisted — browser agents CANNOT visit unlisted domains.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class Source:
    domain: str
    url_template: str
    trust_rank: str
    trust_score: float
    category: str
    notes: str = ""

    def build_url(self, **kwargs) -> str:
        try:
            return self.url_template.format(**kwargs)
        except KeyError:
            return self.url_template

REGISTRY: dict[str, list[Source]] = {
    "gold": [
        Source("goldprice.org",    "https://goldprice.org/gold-price-india.html",           "A",  1.00, "gold"),
        Source("investing.com",    "https://www.investing.com/commodities/gold",              "A",  1.00, "gold"),
        Source("moneycontrol.com", "https://www.moneycontrol.com/commodity/gold-price.html", "A-", 0.92, "gold"),
        Source("goodreturns.in",   "https://www.goodreturns.in/gold-rates/",                 "B+", 0.85, "gold"),
        Source("marketwatch.com",  "https://www.marketwatch.com/investing/future/gold",      "A-", 0.92, "gold"),
        Source("kitco.com",        "https://www.kitco.com/gold-price-today-india.html",      "A",  1.00, "gold"),
    ],
    "silver": [
        Source("goldprice.org",    "https://goldprice.org/silver-price-india.html",           "A",  1.00, "silver"),
        Source("investing.com",    "https://www.investing.com/commodities/silver",             "A",  1.00, "silver"),
        Source("moneycontrol.com", "https://www.moneycontrol.com/commodity/silver-price.html","A-", 0.92, "silver"),
        Source("goodreturns.in",   "https://www.goodreturns.in/silver-rates/",                "B+", 0.85, "silver"),
        Source("kitco.com",        "https://www.kitco.com/silver-price-today-india.html",     "A",  1.00, "silver"),
        Source("marketwatch.com",  "https://www.marketwatch.com/investing/future/silver",     "A-", 0.92, "silver"),
    ],
    "oil": [
        Source("oilprice.com",         "https://oilprice.com/oil-price-charts/",                   "A",  1.00, "oil"),
        Source("tradingeconomics.com",  "https://tradingeconomics.com/commodity/crude-oil",          "A",  1.00, "oil"),
        Source("eia.gov",              "https://www.eia.gov/petroleum/",                            "A",  1.00, "oil", "Official US energy data"),
        Source("marketwatch.com",      "https://www.marketwatch.com/investing/future/crude-oil-wti","A-", 0.92, "oil"),
        Source("reuters.com",          "https://www.reuters.com/markets/commodities/",              "A",  1.00, "oil"),
        Source("investing.com",        "https://www.investing.com/commodities/crude-oil",           "A",  1.00, "oil"),
    ],
    "flight": [
        Source("makemytrip.com",  "https://www.makemytrip.com/flight/search?itinerary={origin}-{destination}-{date}", "A",  1.00, "flight"),
        Source("google.com",      "https://www.google.com/travel/flights",                        "A",  1.00, "flight"),
        Source("skyscanner.com",  "https://www.skyscanner.co.in/flights/{origin}/{destination}/{date}/","A-", 0.92, "flight"),
        Source("ixigo.com",       "https://www.ixigo.com/search/result/flight/{origin}/{destination}/{date}/1/0/0/E/0/0/","B+", 0.85, "flight"),
        Source("cleartrip.com",   "https://www.cleartrip.com/flights/results?from={origin}&to={destination}&depart_date={date}","B+", 0.85, "flight"),
        Source("paytm.com",       "https://paytm.com/flights/{origin}-to-{destination}/{date}/1-adults","B+", 0.85, "flight"),
    ],
    "train": [
        Source("irctc.co.in",    "https://www.irctc.co.in/nget/train-search",                       "A",  1.00, "train", "Official Indian Railways"),
        Source("confirmtkt.com", "https://confirmtkt.com/trains/{origin}/{destination}/{date}",      "A-", 0.92, "train"),
        Source("trainman.in",    "https://trainman.in/trains/{origin}/{destination}",                "B+", 0.85, "train"),
        Source("ixigo.com",      "https://www.ixigo.com/trains/results/{origin}/{destination}/{date}","B+", 0.85, "train"),
        Source("makemytrip.com", "https://www.makemytrip.com/railways/",                            "A-", 0.92, "train"),
        Source("railyatri.in",   "https://www.railyatri.in/trains-between-stations",                "B",  0.70, "train"),
    ],
    "hotel": [
        Source("booking.com",    "https://www.booking.com/searchresults.html?ss={destination}",     "A",  1.00, "hotel"),
        Source("hotels.com",     "https://www.hotels.com/search.do?q-destination={destination}",    "A",  1.00, "hotel"),
        Source("makemytrip.com", "https://www.makemytrip.com/hotels/hotel-listing/?city={destination}","A-", 0.92, "hotel"),
        Source("agoda.com",      "https://www.agoda.com/search?city={destination}",                 "A-", 0.92, "hotel"),
        Source("oyo.com",        "https://www.oyorooms.com/search/?location={destination}",         "B+", 0.85, "hotel"),
        Source("tripadvisor.com","https://www.tripadvisor.com/Hotels-g{destination}",               "B+", 0.85, "hotel"),
    ],
    "weather": [
        Source("weather.com",     "https://weather.com/weather/today",          "A",  1.00, "weather"),
        Source("accuweather.com", "https://www.accuweather.com/en/in/",          "A",  1.00, "weather"),
        Source("imd.gov.in",      "https://mausam.imd.gov.in/",                 "A",  1.00, "weather", "Official India Met Dept"),
        Source("timeanddate.com", "https://www.timeanddate.com/weather/india/",  "A-", 0.92, "weather"),
        Source("windy.com",       "https://www.windy.com/",                      "A-", 0.92, "weather"),
        Source("wunderground.com","https://www.wunderground.com/weather/in/",    "B+", 0.85, "weather"),
    ],
    "stock": [
        Source("nseindia.com",     "https://www.nseindia.com/get-quotes/equity?symbol={symbol}", "A",  1.00, "stock", "NSE official"),
        Source("bseindia.com",     "https://www.bseindia.com/stock-share-price/{symbol}/",       "A",  1.00, "stock", "BSE official"),
        Source("moneycontrol.com", "https://www.moneycontrol.com/india/stockpricequote/",         "A-", 0.92, "stock"),
        Source("investing.com",    "https://www.investing.com/equities/{symbol}",                 "A",  1.00, "stock"),
        Source("marketwatch.com",  "https://www.marketwatch.com/investing/stock/{symbol}",        "A-", 0.92, "stock"),
        Source("screener.in",      "https://www.screener.in/company/{symbol}/",                   "B+", 0.85, "stock"),
    ],
}

DOMAIN_WHITELIST: frozenset[str] = frozenset(
    src.domain for sources in REGISTRY.values() for src in sources
)

def get_sources(category: str, max_count: int = 6) -> list[Source]:
    sources = REGISTRY.get(category, [])
    return sorted(sources, key=lambda s: s.trust_score, reverse=True)[:max_count]

def is_whitelisted(domain: str) -> bool:
    clean = domain.lower().removeprefix("www.")
    return any(
        clean == d.removeprefix("www.") or clean.endswith("." + d.removeprefix("www."))
        for d in DOMAIN_WHITELIST
    )

def detect_category(query: str) -> Optional[str]:
    import re
    q = query.lower()
    patterns = {
        "gold":    [r"\bgold\b"],
        "silver":  [r"\bsilver\b"],
        "oil":     [r"\boil\b", r"\bcrude\b", r"\bwti\b", r"\bpetrol\b"],
        "flight":  [r"\bflight\b", r"\bfly\b", r"\bairfare\b"],
        "train":   [r"\btrain\b", r"\brailway\b", r"\birctc\b"],
        "hotel":   [r"\bhotel\b", r"\baccommodation\b", r"\bstay\b"],
        "weather": [r"\bweather\b", r"\btemperature\b", r"\bforecast\b"],
        "stock":   [r"\bstock\b", r"\bshare\b", r"\bnifty\b", r"\bsensex\b"],
    }
    for category, pats in patterns.items():
        if any(re.search(p, q, re.I) for p in pats):
            return category
    return None
