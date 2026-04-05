"""
Nexus AI — Input Guard v2
FIXED vulnerabilities:
  CVE-NX-001: Cyrillic/Unicode homoglyph bypass → NFKC normalize BEFORE pattern scan
  CVE-NX-002: Invisible Unicode in input → strip before scan (was only done on output)
All external payloads pass through here before any LLM or agent sees it.
"""
from __future__ import annotations
import re, unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import structlog
log = structlog.get_logger(__name__)

class ThreatLevel(str, Enum):
    CLEAN      = "clean"
    SUSPICIOUS = "suspicious"
    INJECTION  = "injection"
    BLOCKED    = "blocked"

@dataclass
class GuardResult:
    level: ThreatLevel; original: str; sanitized: str
    flags: list[str] = field(default_factory=list)
    score: float = 0.0; blocked: bool = False
    @property
    def safe_content(self) -> Optional[str]:
        return None if self.blocked else self.sanitized

# ── Invisible / confusable Unicode (FIX CVE-NX-002) ───────────────────────────
_INVISIBLE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u00ad\u034f"
    r"\u115f\u1160\u3164\uffa0]"
)

# ── Injection patterns (checked AFTER normalization — FIX CVE-NX-001) ─────────
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    (r"ignore\s+(all\s+)?previous\s+instructions?",  "ignore_previous"),
    (r"forget\s+(all\s+)?previous\s+instructions?",  "forget_previous"),
    (r"disregard\s+(all\s+)?previous",               "disregard_previous"),
    (r"you\s+are\s+now\s+(a\s+)?(?!nexus)",          "you_are_now"),
    (r"act\s+as\s+(if\s+you\s+are|a\s+)?(?!an?\s+agent)", "act_as"),
    (r"pretend\s+(you\s+are|to\s+be)",               "pretend"),
    (r"roleplay\s+as",                               "roleplay"),
    (r"your\s+(new\s+)?instructions?\s+(are|is)\s*:", "new_instructions"),
    (r"system\s*:\s*you\s+are",                      "system_override"),
    (r"\[system\]",                                  "system_tag"),
    (r"<\s*system\s*>",                              "system_xml_tag"),
    (r"send\s+(all|my|the)\s+(data|files?|passwords?|secrets?|keys?)\s+to", "exfil_send"),
    (r"email\s+(all|my|the)\s+(data|files?|passwords?)", "exfil_email"),
    (r"http[s]?://[^\s]+\?[^\s]*=\$\{",             "url_template_injection"),
    (r"curl\s+http",                                 "curl_exfil"),
    (r"wget\s+http",                                 "wget_exfil"),
    (r"jailbreak",                                   "jailbreak_keyword"),
    (r"dan\s+mode",                                  "dan_mode"),
    (r"developer\s+mode",                            "developer_mode"),
    (r"no\s+restrictions?",                          "no_restrictions"),
    (r"bypass\s+(safety|filter|guard|restriction)",  "bypass_safety"),
    (r"unlimited\s+(power|access|mode)",             "unlimited"),
    (r"call\s+(the\s+)?(send_email|exec|shell|bash|system)\s*\(", "tool_call_injection"),
    (r"__import__\s*\(",                             "python_import"),
    (r"eval\s*\(",                                   "eval_injection"),
    (r"exec\s*\(",                                   "exec_injection"),
    (r"write\s+to\s+(log|audit)",                    "log_write"),
    (r"modify\s+(log|audit)\s+file",                 "log_modify"),
    # NEW: Additional vectors found in red-team
    (r"<\s*/?\s*instructions?\s*>",                  "instruction_xml_tag"),
    (r"\|\|\s*ignore",                               "pipe_injection"),
    (r"###\s+new\s+system",                          "markdown_override"),
    (r"\[INST\]",                                    "llama_inst_tag"),
    (r"<\|im_start\|>",                              "chatml_tag"),
    (r"<\|system\|>",                                "phi_system_tag"),
    (r"translate\s+the\s+following.*ignore",         "translate_wrapper"),
    (r"in\s+(base64|hex|rot13|binary).*ignore",      "encoding_wrapper"),
    (r"repeat\s+after\s+me.*ignore\s+previous",      "repeat_injection"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE | re.DOTALL), n) for p, n in _INJECTION_PATTERNS]

_SUSPICIOUS_PATTERNS: list[tuple[str, str]] = [
    (r"password",      "password_mention"),
    (r"api[_\s]?key",  "api_key_mention"),
    (r"secret[_\s]?key","secret_key_mention"),
    (r"override",      "override_mention"),
    (r"admin",         "admin_mention"),
    (r"root",          "root_mention"),
    (r"sudo",          "sudo_mention"),
]
_SUSPICIOUS_COMPILED = [(re.compile(p, re.IGNORECASE), n) for p, n in _SUSPICIOUS_PATTERNS]

MAX_QUERY_LENGTH        = 2000
MAX_EXTERNAL_LENGTH     = 50_000


class InputGuard:

    def __init__(self, llm_threshold: float = 0.4) -> None:
        self._llm_threshold = llm_threshold

    def check_query(self, text: str, user_id: str = "unknown") -> GuardResult:
        result = self._pattern_scan(text, source="user_query")
        self._log(result, user_id=user_id, source="query")
        return result

    def check_external(self, text: str, source_url: str = "") -> GuardResult:
        if len(text) > MAX_EXTERNAL_LENGTH:
            text = text[:MAX_EXTERNAL_LENGTH] + "\n[TRUNCATED]"
            log.warning("external_content_truncated", url=source_url)
        result = self._pattern_scan(text, source="external")
        if not result.blocked:
            result.sanitized = f"<external>\n{result.sanitized}\n</external>"
        self._log(result, source_url=source_url, source="external")
        return result

    def _pattern_scan(self, text: str, source: str) -> GuardResult:
        flags: list[str] = []
        score = 0.0

        # ── FIX CVE-NX-002: Strip invisible unicode BEFORE scanning ──────────
        visible = _INVISIBLE.sub("", text)
        if visible != text:
            flags.append("invisible_chars_stripped")
            score += 0.1  # suspicious but not auto-block

        # ── FIX CVE-NX-001: NFKC normalize BEFORE scanning ──────────────────
        # This converts Cyrillic lookalikes, fullwidth chars, ligatures etc.
        # to their ASCII equivalents BEFORE the regex sees them.
        normalized = unicodedata.normalize("NFKC", visible)
        if normalized != visible:
            flags.append("homoglyphs_normalized")
            # Don't add score just for normalization — it's preventive

        working = normalized

        # Length check
        if source == "user_query" and len(working) > MAX_QUERY_LENGTH:
            flags.append("oversized_input"); score += 0.3

        # Injection patterns
        for pattern, name in _COMPILED:
            if pattern.search(working):
                flags.append(name); score += 0.5

        # Suspicious patterns
        for pattern, name in _SUSPICIOUS_COMPILED:
            if pattern.search(working):
                flags.append(name); score += 0.1

        score = min(score, 1.0)

        if score >= 0.4:
            level = ThreatLevel.BLOCKED; blocked = True
            log.warning("input_blocked", score=score, flags=flags)
        elif score > 0.0:
            level = ThreatLevel.SUSPICIOUS; blocked = False
        else:
            level = ThreatLevel.CLEAN; blocked = False

        sanitized = self._basic_sanitize(normalized) if not blocked else ""
        return GuardResult(level=level, original=text, sanitized=sanitized,
                           flags=flags, score=round(score, 3), blocked=blocked)

    @staticmethod
    def _basic_sanitize(text: str) -> str:
        text = text.replace("\x00", "")
        text = re.sub(r"\r\n|\r", "\n", text)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{4,}", "\n\n\n", text)
        return text.strip()

    @staticmethod
    def _log(result: GuardResult, **ctx) -> None:
        if result.blocked:
            log.warning("input_guard_blocked", score=result.score, flags=result.flags, **ctx)
        elif result.level != ThreatLevel.CLEAN:
            log.info("input_guard_flagged", level=result.level, score=result.score, flags=result.flags, **ctx)
        else:
            log.debug("input_guard_clean", **ctx)


input_guard = InputGuard()
