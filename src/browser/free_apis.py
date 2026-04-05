"""
Nexus AI — Free Data APIs (no credit card, no paid plan)
All sources here are genuinely free with no billing required.
"""
from __future__ import annotations
import asyncio, json, time
from typing import Optional
import structlog

log = structlog.get_logger(__name__)

# ── Free API endpoints ─────────────────────────────────────────────────────────

class FreeDataAPIs:
    """
    Real free API sources. No fake data. No hardcoded prices.
    All return LIVE data when called on a machine with internet.
    """

    async def get_gold_usd(self) -> Optional[float]:
        """
        metals.live — completely free, no API key.
        Returns current gold price in USD per troy oz.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://api.metals.live/v1/spot/gold",
                                headers={"User-Agent": "NexusAI/2.0"})
                data = r.json()
                return float(data.get("gold", 0))
        except Exception as exc:
            log.warning("metals_live_failed", error=str(exc))
            return None

    async def get_usd_inr_rate(self) -> Optional[float]:
        """
        exchangerate-api.com — free tier, no key for basic pairs.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://open.er-api.com/v6/latest/USD",
                                headers={"User-Agent": "NexusAI/2.0"})
                data = r.json()
                return float(data["rates"].get("INR", 0))
        except Exception as exc:
            log.warning("exchangerate_failed", error=str(exc))
            return None

    async def gold_price_inr_per_10g(self) -> Optional[dict]:
        """
        Compute gold price in INR per 10g using free APIs.
        USD/troy_oz × USD/INR rate × (10g / 31.1035g per troy oz)
        """
        usd_per_oz, usd_inr = await asyncio.gather(
            self.get_gold_usd(),
            self.get_usd_inr_rate(),
        )
        if not usd_per_oz or not usd_inr:
            return None

        grams_per_oz = 31.1035
        price_inr_per_10g = int(usd_per_oz * usd_inr * (10 / grams_per_oz))
        return {
            "price_inr_per_10g": price_inr_per_10g,
            "display": f"₹{price_inr_per_10g:,}",
            "unit": "per 10g (24k)",
            "source": "metals.live + open.er-api.com",
            "usd_per_oz": round(usd_per_oz, 2),
            "usd_inr_rate": round(usd_inr, 4),
            "note": "Computed from spot price — may differ slightly from retail jewellery price",
            "confidence": 0.90,
        }

    async def get_nifty_50(self) -> Optional[dict]:
        """
        NSE India has a public JSON endpoint — no API key needed.
        """
        try:
            import httpx
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.nseindia.com/",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient(timeout=10, headers=headers) as c:
                # Get a session cookie first
                await c.get("https://www.nseindia.com/")
                r = await c.get(
                    "https://www.nseindia.com/api/allIndices",
                    headers=headers,
                )
                data = r.json()
                for idx in data.get("data", []):
                    if idx.get("indexSymbol") == "NIFTY 50":
                        return {
                            "index": "NIFTY 50",
                            "last": idx["last"],
                            "change": idx["variation"],
                            "change_pct": idx["percentChange"],
                            "open": idx["open"],
                            "high": idx["high"],
                            "low": idx["low"],
                            "source": "nseindia.com",
                        }
        except Exception as exc:
            log.warning("nse_api_failed", error=str(exc))
            return None

    async def get_weather(self, city: str) -> Optional[dict]:
        """
        Open-Meteo — completely free, no API key, no rate limits.
        """
        try:
            import httpx
            # First get lat/lon for the city
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=en&format=json"
            async with httpx.AsyncClient(timeout=10) as c:
                geo = await c.get(geo_url)
                geo_data = geo.json()
                if not geo_data.get("results"):
                    return None
                loc = geo_data["results"][0]
                lat, lon = loc["latitude"], loc["longitude"]

                # Get weather
                weather_url = (
                    f"https://api.open-meteo.com/v1/forecast"
                    f"?latitude={lat}&longitude={lon}"
                    f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
                    f"&timezone=Asia/Kolkata"
                )
                wr = await c.get(weather_url)
                wd = wr.json()
                cur = wd["current"]
                return {
                    "city": loc["name"],
                    "temp_c": cur["temperature_2m"],
                    "humidity_pct": cur["relative_humidity_2m"],
                    "wind_kmh": cur["wind_speed_10m"],
                    "source": "open-meteo.com (free, no key)",
                }
        except Exception as exc:
            log.warning("weather_failed", error=str(exc))
            return None

    async def get_currency_rate(self, from_curr: str, to_curr: str) -> Optional[dict]:
        """
        open.er-api.com — free tier, no key required for basic pairs.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"https://open.er-api.com/v6/latest/{from_curr.upper()}")
                data = r.json()
                rate = data["rates"].get(to_curr.upper())
                if not rate:
                    return None
                return {
                    "from": from_curr.upper(),
                    "to": to_curr.upper(),
                    "rate": round(rate, 4),
                    "source": "open.er-api.com",
                    "updated": data.get("time_last_update_utc"),
                }
        except Exception as exc:
            log.warning("currency_failed", error=str(exc))
            return None


free_apis = FreeDataAPIs()
