"""
Nexus AI — PII masker.

Runs Microsoft Presidio on every outbound message before it leaves the system.
Catches: email addresses, phone numbers, names, Aadhaar, PAN, credit cards,
         API keys, passwords, IP addresses, and more.

Applied at:
  - Agent output → user
  - Drafter output → Telegram / email
  - Audit log entries (partial masking, preserves structure)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class MaskResult:
    original: str
    masked: str
    entities_found: list[str]
    was_modified: bool

    @property
    def safe_text(self) -> str:
        return self.masked


# ── Custom patterns for India-specific PII ────────────────────────────────────

_CUSTOM_PATTERNS = [
    # Aadhaar (12-digit, optionally spaced in groups of 4)
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[AADHAAR]"),
    # PAN card
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[PAN]"),
    # Indian phone numbers
    (re.compile(r"\b(?:\+91[\s-]?)?[6-9]\d{9}\b"), "[PHONE]"),
    # API keys (generic — long alphanumeric strings after key=, token=, secret=)
    (re.compile(
        r'(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*["\']?([A-Za-z0-9\-_\.]{20,})["\']?',
        re.IGNORECASE
    ), "[REDACTED_CREDENTIAL]"),
    # Bearer tokens in headers
    (re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]+", re.IGNORECASE), "Bearer [TOKEN]"),
    # AWS-style keys
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[AWS_KEY]"),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b"), "[EMAIL]"),
    # IPv4
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP_ADDRESS]"),
]


class PIIMasker:
    """
    Two-pass PII masker:
      Pass 1 — Custom regex patterns (fast, India-specific)
      Pass 2 — Presidio (NER-based, catches names, orgs, locations)

    Presidio is loaded lazily on first use to avoid slow startup.
    If Presidio is unavailable (e.g. spacy model not downloaded),
    falls back to regex-only mode with a warning.
    """

    def __init__(self) -> None:
        self._presidio_ready = False
        self._analyzer = None
        self._anonymizer = None

    def _load_presidio(self) -> bool:
        if self._presidio_ready:
            return True
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
            self._analyzer = AnalyzerEngine()
            self._anonymizer = AnonymizerEngine()
            self._presidio_ready = True
            log.info("presidio_loaded")
            return True
        except Exception as exc:
            log.warning("presidio_unavailable", error=str(exc), fallback="regex_only")
            return False

    # ── Public API ─────────────────────────────────────────────────────────────

    def mask(self, text: str, *, language: str = "en") -> MaskResult:
        """
        Mask all PII in text. Safe to call on any string before it leaves
        the system (agent output, email draft, Telegram message, log entry).
        """
        if not text or not text.strip():
            return MaskResult(original=text, masked=text, entities_found=[], was_modified=False)

        entities: list[str] = []
        working = text

        # Pass 1 — custom regex
        working, regex_entities = self._regex_pass(working)
        entities.extend(regex_entities)

        # Pass 2 — Presidio (if available)
        if self._load_presidio():
            working, presidio_entities = self._presidio_pass(working, language)
            entities.extend(presidio_entities)

        was_modified = working != text
        if was_modified:
            log.info("pii_masked", entity_types=list(set(entities)), count=len(entities))

        return MaskResult(
            original=text,
            masked=working,
            entities_found=list(set(entities)),
            was_modified=was_modified,
        )

    def mask_for_log(self, text: str) -> str:
        """
        Lighter masking for audit log entries — preserves structure,
        only replaces the most sensitive PII types.
        """
        result = text
        for pattern, replacement in _CUSTOM_PATTERNS[:4]:  # first 4 are most sensitive
            result = pattern.sub(replacement, result)
        return result

    # ── Internal passes ────────────────────────────────────────────────────────

    @staticmethod
    def _regex_pass(text: str) -> tuple[str, list[str]]:
        entities: list[str] = []
        for pattern, replacement in _CUSTOM_PATTERNS:
            if pattern.search(text):
                label = replacement.strip("[]")
                entities.append(label)
                text = pattern.sub(replacement, text)
        return text, entities

    def _presidio_pass(self, text: str, language: str) -> tuple[str, list[str]]:
        try:
            results = self._analyzer.analyze(
                text=text,
                language=language,
                entities=["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
                          "IBAN_CODE", "LOCATION", "NRP", "DATE_TIME"],
            )
            if not results:
                return text, []
            anonymized = self._anonymizer.anonymize(text=text, analyzer_results=results)
            entity_types = [r.entity_type for r in results]
            return anonymized.text, entity_types
        except Exception as exc:
            log.warning("presidio_pass_failed", error=str(exc))
            return text, []


# Module-level singleton
pii_masker = PIIMasker()
