"""
Nexus AI — Browser Agent.

One instance per source URL. 6 instances run in parallel via asyncio.

Features:
  - Headless Chromium via Playwright
  - playwright-stealth: evades bot detection
  - networkidle wait: ensures JS-rendered content loads
  - DOM integrity check: detects CAPTCHA / 403 / block pages
  - LLM selector healing: if hardcoded selector fails, Qwen2.5-72B picks a new one
  - Screenshot evidence: every scrape saves a PNG for audit
  - Structured JSON output: value, unit, timestamp, source_url, screenshot_path

Security:
  - No private data access (no vector_search, no gmail_read)
  - No external comms (cannot send_email, telegram_send)
  - Reads untrusted web content ONLY — wrapped in <external> tags before LLM sees it
  - Tool manifest: [browser_scrape] only
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.browser.site_registry import SourceEntry
from src.security.input_guard import input_guard

log = structlog.get_logger(__name__)
_settings = get_settings()


# ── Scrape result ─────────────────────────────────────────────────────────────

@dataclass
class ScrapeResult:
    source_entry: SourceEntry
    status: str                     # "valid" | "blocked" | "error" | "empty"
    raw_value: Optional[str] = None
    structured: Optional[dict] = None
    screenshot_path: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    latency_ms: float = 0.0
    error_msg: str = ""
    dom_flags: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.status == "valid" and self.raw_value is not None

    @property
    def source_name(self) -> str:
        return self.source_entry.name

    @property
    def trust_score(self) -> float:
        return self.source_entry.trust_score


# ── Block signal detection ─────────────────────────────────────────────────────

_BLOCK_SIGNALS = [
    "captcha", "access denied", "403 forbidden", "403 error",
    "blocked", "cloudflare", "verify you are human",
    "enable javascript", "too many requests", "rate limit",
    "bot detection", "unusual traffic", "please wait",
    "checking your browser", "ddos", "security check",
]

_BLOCK_TITLE_SIGNALS = [
    "attention required", "just a moment", "403", "access denied",
    "blocked", "captcha", "security check",
]


def _detect_block(title: str, body_text: str) -> list[str]:
    """Return list of block signal names found, empty if clean."""
    flags = []
    t = title.lower()
    b = body_text.lower()[:5000]   # only check first 5000 chars
    for sig in _BLOCK_TITLE_SIGNALS:
        if sig in t:
            flags.append(f"title:{sig}")
    for sig in _BLOCK_SIGNALS:
        if sig in b:
            flags.append(f"body:{sig}")
    return flags


# ── Selector picker (LLM-based, self-healing) ─────────────────────────────────

_SELECTOR_SYSTEM = """You are a CSS/text selector picker for web scraping.
Given a webpage's text content and a hint, identify the CSS selector or text pattern
that contains the target price or data value.

Return ONLY a JSON object:
{"selector": "CSS selector or text pattern", "method": "css|text|regex", "confidence": 0.0-1.0}

No other text. No markdown. JSON only."""


async def _llm_pick_selector(
    page_text: str,
    hint: str,
    query: str,
    llm_client,
) -> Optional[dict]:
    """Ask Qwen2.5-72B to pick the right selector from page content."""
    # Wrap page content as external (untrusted)
    wrapped = f"<external>\n{page_text[:3000]}\n</external>"
    prompt = (
        f"Query: {query}\n"
        f"Selector hint from site registry: {hint}\n"
        f"Page text:\n{wrapped}"
    )
    try:
        response = await llm_client.chat(
            model=_settings.researcher_model,
            messages=[{"role": "user", "content": prompt}],
            system=_SELECTOR_SYSTEM,
            temperature=0.0,
            max_tokens=128,
            json_mode=True,
        )
        return json.loads(response.content)
    except Exception as exc:
        log.warning("selector_llm_failed", error=str(exc))
        return None


# ── Browser Agent ─────────────────────────────────────────────────────────────

class BrowserAgent:
    """
    Single-source browser scraper.
    Instantiate one per source URL. Run all 6 with asyncio.gather().
    """

    TOOL = "browser_scrape"

    def __init__(
        self,
        source: SourceEntry,
        query: str,
        query_category: str,
        llm_client=None,
        screenshot_dir: Optional[Path] = None,
    ) -> None:
        self._source = source
        self._query = query
        self._category = query_category
        self._screenshot_dir = screenshot_dir or _settings.screenshots_dir
        self._id = str(uuid.uuid4())[:8]
        from src.agents.llm_client import llm_client as _lc
        self._llm = llm_client or _lc
        self._log = log.bind(source=source.name, id=self._id)

    async def scrape(self) -> ScrapeResult:
        """
        Full scrape lifecycle:
          1. Launch browser (stealth)
          2. Navigate + wait
          3. DOM integrity check
          4. Extract value (hardcoded selector → LLM healing)
          5. Screenshot
          6. Return structured result
        """
        t0 = time.perf_counter()
        self._log.info("browser_scrape_start", url=self._source.url)

        try:
            result = await self._do_scrape()
        except Exception as exc:
            result = ScrapeResult(
                source_entry=self._source,
                status="error",
                error_msg=str(exc),
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            self._log.error("browser_scrape_error", error=str(exc))

        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        self._log.info(
            "browser_scrape_done",
            status=result.status,
            ms=result.latency_ms,
            value=result.raw_value,
        )
        return result

    async def _do_scrape(self) -> ScrapeResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return await self._mock_scrape()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="Asia/Kolkata",
            )

            # Apply stealth if available
            try:
                from playwright_stealth import stealth_async
                page = await context.new_page()
                await stealth_async(page)
            except ImportError:
                page = await context.new_page()

            # Navigate
            wait_until = "networkidle" if self._source.requires_js else "load"
            try:
                await page.goto(
                    self._source.url,
                    wait_until=wait_until,
                    timeout=_settings.browser_timeout_ms,
                )
            except Exception as exc:
                await browser.close()
                return ScrapeResult(
                    source_entry=self._source,
                    status="error",
                    error_msg=f"Navigation failed: {exc}",
                )

            # DOM integrity check
            title = await page.title()
            body_text = await page.inner_text("body")
            dom_flags = _detect_block(title, body_text)

            if dom_flags:
                screenshot_path = await self._take_screenshot(page)
                await browser.close()
                return ScrapeResult(
                    source_entry=self._source,
                    status="blocked",
                    dom_flags=dom_flags,
                    screenshot_path=screenshot_path,
                    error_msg=f"Block detected: {dom_flags}",
                )

            # Extract value
            raw_value = await self._extract_value(page, body_text)
            screenshot_path = await self._take_screenshot(page)
            await browser.close()

            if raw_value is None:
                return ScrapeResult(
                    source_entry=self._source,
                    status="empty",
                    screenshot_path=screenshot_path,
                    error_msg="No value extracted",
                )

            # Run input guard on scraped text (treat as external content)
            guard = input_guard.check_external(raw_value, self._source.url)
            if guard.blocked:
                return ScrapeResult(
                    source_entry=self._source,
                    status="blocked",
                    dom_flags=["injection_in_scraped_content"],
                    error_msg="Injection pattern detected in scraped content",
                )

            structured = self._structure_value(raw_value)
            return ScrapeResult(
                source_entry=self._source,
                status="valid",
                raw_value=raw_value,
                structured=structured,
                screenshot_path=screenshot_path,
                timestamp=time.time(),
            )

    async def _extract_value(self, page, body_text: str) -> Optional[str]:
        """Try hardcoded selector hint first, then LLM healing."""
        hint = self._source.selector_hint

        # Try hardcoded hint (CSS class or text contains)
        try:
            el = page.locator(f".{hint}").first
            text = await el.inner_text(timeout=3000)
            if text and len(text.strip()) > 0:
                return text.strip()
        except Exception:
            pass

        # LLM healing — pick selector from page content
        self._log.info("selector_healing", hint=hint)
        selector_info = await _llm_pick_selector(
            body_text, hint, self._query, self._llm
        )
        if selector_info and selector_info.get("confidence", 0) > 0.6:
            try:
                method = selector_info.get("method", "css")
                sel = selector_info["selector"]
                if method == "css":
                    el = page.locator(sel).first
                    text = await el.inner_text(timeout=3000)
                    return text.strip() if text else None
                elif method == "text":
                    # Find text in page
                    matches = re.findall(sel, body_text)
                    return matches[0] if matches else None
            except Exception as exc:
                self._log.warning("healed_selector_failed", error=str(exc))

        # Last resort: regex search on body text for price patterns
        return self._regex_extract(body_text)

    def _regex_extract(self, text: str) -> Optional[str]:
        """Last-resort regex extraction for price patterns."""
        patterns = [
            r"₹\s*[\d,]+(?:\.\d+)?",          # Indian rupee
            r"\$\s*[\d,]+(?:\.\d+)?",          # USD
            r"[\d,]+(?:\.\d+)?\s*per\s+\w+",  # "71211 per 10g"
            r"[\d,]+\.\d{2}",                  # decimal price
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(0).strip()
        return None

    async def _take_screenshot(self, page) -> Optional[str]:
        """Save screenshot as evidence."""
        try:
            self._screenshot_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{self._category}_{self._source.name.replace('.', '_')}_{self._id}.png"
            path = self._screenshot_dir / filename
            await page.screenshot(
                path=str(path),
                clip={"x": 0, "y": 0, "width": 1280, "height": 600},
            )
            return str(path)
        except Exception as exc:
            self._log.warning("screenshot_failed", error=str(exc))
            return None

    def _structure_value(self, raw: str) -> dict:
        """Parse raw text into structured numeric + unit fields."""
        # Extract numeric part
        num_match = re.search(r"[\d,]+\.?\d*", raw.replace(",", ""))
        value = float(num_match.group(0)) if num_match else None

        # Detect currency/unit
        currency = None
        if "₹" in raw or "INR" in raw.upper():
            currency = "INR"
        elif "$" in raw or "USD" in raw.upper():
            currency = "USD"
        elif "£" in raw:
            currency = "GBP"
        elif "€" in raw:
            currency = "EUR"

        return {
            "value": value,
            "raw": raw,
            "currency": currency,
            "source_url": self._source.url,
            "source_name": self._source.name,
            "trust_score": self._source.trust_score,
            "timestamp": time.time(),
        }

    async def _mock_scrape(self) -> ScrapeResult:
        """
        Mock scraper for development when Playwright is not installed.
        Returns realistic simulated data.
        """
        await asyncio.sleep(0.1 + hash(self._source.name) % 5 * 0.05)

        MOCK_VALUES = {
            "gold":    {"goldprice.org": "₹71,240", "investing.com": "₹71,185",
                        "moneycontrol.com": "₹71,210", "goodreturns.in": "₹71,198",
                        "marketwatch.com": "₹71,220", "kitco.com": None},  # None = blocked
            "oil":     {"oilprice.com": "$78.42", "tradingeconomics.com": "$78.38",
                        "eia.gov": "$78.45", "marketwatch.com": "$78.41",
                        "investing.com": "$78.44", "yahoo finance": None},
            "flight":  {"makemytrip.com": "₹4,299 IndiGo 06:05",
                        "google flights": "₹4,450 Air India 08:30",
                        "skyscanner.com": "₹4,180 SpiceJet 07:15",
                        "ixigo.com": "₹4,320 IndiGo 09:40",
                        "paytm travel": "₹4,195 SpiceJet 11:05",
                        "cleartrip.com": None},
        }

        cat_values = MOCK_VALUES.get(self._category, {})
        raw = cat_values.get(self._source.name)

        if raw is None:
            return ScrapeResult(
                source_entry=self._source,
                status="blocked",
                dom_flags=["mock:captcha"],
                error_msg="Mock: CAPTCHA simulation",
                screenshot_path=None,
            )

        return ScrapeResult(
            source_entry=self._source,
            status="valid",
            raw_value=raw,
            structured=self._structure_value(raw),
            screenshot_path=None,
            timestamp=time.time(),
        )
