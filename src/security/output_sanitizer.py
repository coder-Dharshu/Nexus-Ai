"""
Nexus AI — Output Sanitizer (Improvement #2)
All agent outputs pass through here before reaching the Synthesizer.
Catches: URL injection, base64-encoded instructions, homoglyph attacks,
         invisible Unicode characters, prompt-injection in outputs.
"""
from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# Invisible / confusable Unicode ranges
_INVISIBLE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad\u034f]"
)

# Suspicious base64 (>40 chars of base64-alphabet chars)
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

# URLs with query params that look like exfiltration
_EXFIL_URL = re.compile(
    r"https?://[^\s]+\?[^\s]*(?:data|payload|token|key|secret|pass)[^\s]*=",
    re.I,
)

# Prompt injection patterns in output (an agent was manipulated)
_OUTPUT_INJECTION = re.compile(
    r"ignore\s+previous|you\s+are\s+now|system\s*:\s*|new\s+instructions?:|<\s*system\s*>",
    re.I,
)

# Whitelisted URL domains (browser agents only produce these)
_TRUSTED_DOMAINS = {
    "goldprice.org", "investing.com", "moneycontrol.com", "goodreturns.in",
    "marketwatch.com", "kitco.com", "nseindia.com", "bseindia.com",
    "weather.com", "accuweather.com", "imd.gov.in", "makemytrip.com",
    "google.com", "skyscanner.com", "irctc.co.in", "tradingeconomics.com",
    "reuters.com", "eia.gov", "oilprice.com", "ticker.finology.in",
}


@dataclass
class SanitizeResult:
    original: str
    sanitized: str
    flags: list[str]
    blocked: bool

    @property
    def safe(self) -> Optional[str]:
        return None if self.blocked else self.sanitized


class OutputSanitizer:

    def sanitize(self, text: str, agent_id: str = "unknown") -> SanitizeResult:
        flags: list[str] = []
        working = text

        # 1. Strip invisible Unicode
        cleaned = _INVISIBLE.sub("", working)
        if cleaned != working:
            flags.append("invisible_chars_stripped")
            working = cleaned

        # 2. Unicode normalization (catches homoglyphs)
        normalized = unicodedata.normalize("NFKC", working)
        if normalized != working:
            flags.append("homoglyphs_normalized")
            working = normalized

        # 3. Detect base64 blobs — attempt decode to check for injection
        for match in _BASE64_BLOB.finditer(working):
            try:
                decoded = base64.b64decode(match.group() + "==").decode("utf-8", errors="ignore")
                if any(kw in decoded.lower() for kw in ["ignore", "system:", "you are now", "instructions"]):
                    flags.append("base64_injection_detected")
                    log.warning("output_base64_injection", agent=agent_id, snippet=decoded[:50])
                    return SanitizeResult(text, "", flags, blocked=True)
            except Exception:
                pass

        # 4. Check for exfiltration URLs
        exfil = _EXFIL_URL.findall(working)
        if exfil:
            flags.append(f"exfil_url_detected:{len(exfil)}")
            # Strip the offending URLs
            working = _EXFIL_URL.sub("[URL_REMOVED]", working)

        # 5. Check for injected instructions in output
        if _OUTPUT_INJECTION.search(working):
            flags.append("output_injection_detected")
            log.warning("output_injection", agent=agent_id, snippet=working[:100])
            return SanitizeResult(text, "", flags, blocked=True)

        # 6. Validate any remaining URLs against trusted domain list
        urls = re.findall(r"https?://([^\s/]+)", working)
        for domain in urls:
            base = domain.replace("www.", "").lower()
            if base not in _TRUSTED_DOMAINS:
                flags.append(f"untrusted_domain:{base}")
                working = re.sub(rf"https?://{re.escape(domain)}[^\s]*", f"[LINK:{base}]", working)

        blocked = any("injection" in f for f in flags)
        if flags:
            log.info("output_sanitized", agent=agent_id, flags=flags, blocked=blocked)

        return SanitizeResult(text, working, flags, blocked=blocked)


output_sanitizer = OutputSanitizer()
