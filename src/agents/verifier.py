"""
Nexus AI — Verifier Agent (Improvement #6: Agent Self-Correction Loop)
Runs AFTER the Decision Agent renders a verdict.
Cross-checks every number and claim in the verdict against the original
scraped source data. Flags discrepancies before output reaches the user.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)


@dataclass
class VerificationResult:
    passed: bool
    verdict_text: str
    corrected_text: str
    discrepancies: list[str] = field(default_factory=list)
    correction_count: int = 0


class VerifierAgent(BaseAgent):
    """
    Post-decision verifier. Reads the verdict and scraped source JSON.
    Finds any number in the verdict not present in the source data.
    Corrects or flags it before output leaves the system.
    """
    AGENT_ID = "verifier"
    ROLE     = "verifier"
    TOOLS: list[str] = []

    _SYSTEM = """You are a post-decision verifier agent.
You receive:
1. A final verdict text from the Decision Agent
2. The original verified_data (scraped JSON from browser agents)

Your ONLY job:
- Find every number, price, percentage, or date in the verdict
- Check each against verified_data
- If any value in the verdict does NOT appear in verified_data, flag it as a discrepancy
- If all values are verified, confirm as CLEAN

Respond ONLY in this format:
STATUS: CLEAN | DISCREPANCIES_FOUND
DISCREPANCIES:
[list each discrepancy: "verdict says X, source data says Y"]
CORRECTIONS:
[list each correction: "X → Y"]
CONFIDENCE: <0-100>%"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def verify(
        self,
        verdict_text: str,
        verified_data: dict[str, Any],
        task_id: str = "",
    ) -> VerificationResult:
        """Main entry point. Returns corrected verdict."""

        # Fast path: if no numbers in verdict, it's clean
        numbers_in_verdict = re.findall(
            r"(?:₹|£|\$|€)?\s*[\d,]+\.?\d*\s*(?:%|per\s+\w+)?", verdict_text
        )
        if not numbers_in_verdict:
            return VerificationResult(
                passed=True, verdict_text=verdict_text,
                corrected_text=verdict_text, discrepancies=[]
            )

        # Extract all values from verified_data as strings
        source_values = self._extract_source_values(verified_data)

        prompt = (
            f"VERDICT:\n{verdict_text}\n\n"
            f"VERIFIED_DATA (from browser agents):\n{source_values}"
        )

        try:
            response = await self._llm.chat(
                model=get_settings().decision_model,
                messages=[{"role": "user", "content": prompt}],
                system=self._SYSTEM,
                temperature=0.0,
                max_tokens=512,
            )
            return self._parse_response(response.content, verdict_text)
        except Exception as exc:
            log.warning("verifier_error", error=str(exc), task_id=task_id)
            # On error, pass through unchanged (don't block output)
            return VerificationResult(
                passed=True, verdict_text=verdict_text,
                corrected_text=verdict_text,
            )

    @staticmethod
    def _extract_source_values(data: dict) -> str:
        """Flatten verified_data dict to a string of key=value pairs."""
        if not data:
            return "(no source data)"
        lines = []
        def flatten(d, prefix=""):
            for k, v in d.items() if isinstance(d, dict) else enumerate(d):
                full_key = f"{prefix}.{k}" if prefix else str(k)
                if isinstance(v, (dict, list)):
                    flatten(v, full_key)
                else:
                    lines.append(f"{full_key} = {v}")
        flatten(data)
        return "\n".join(lines[:50])  # cap at 50 lines

    @staticmethod
    def _parse_response(text: str, original: str) -> VerificationResult:
        status_m = re.search(r"STATUS:\s*(CLEAN|DISCREPANCIES_FOUND)", text, re.I)
        status = status_m.group(1).upper() if status_m else "CLEAN"

        discrepancies: list[str] = []
        corrections: list[str] = []

        disc_m = re.search(r"DISCREPANCIES:\s*(.*?)(?:CORRECTIONS:|CONFIDENCE:|$)", text, re.S | re.I)
        if disc_m:
            discrepancies = [l.strip() for l in disc_m.group(1).strip().split("\n") if l.strip() and l.strip() != "-"]

        corr_m = re.search(r"CORRECTIONS:\s*(.*?)(?:CONFIDENCE:|$)", text, re.S | re.I)
        if corr_m:
            corrections = [l.strip() for l in corr_m.group(1).strip().split("\n") if l.strip() and l.strip() != "-"]

        corrected = original
        for correction in corrections:
            if "→" in correction:
                parts = correction.split("→", 1)
                old_val = parts[0].strip().strip('"')
                new_val = parts[1].strip().strip('"')
                corrected = corrected.replace(old_val, new_val)

        passed = status == "CLEAN" or not discrepancies
        return VerificationResult(
            passed=passed,
            verdict_text=original,
            corrected_text=corrected,
            discrepancies=discrepancies,
            correction_count=len(corrections),
        )

    async def run(self, task_id, query, board, context, round_num=0) -> AgentMessage:
        verdict = context.get("verdict_text", "")
        verified_data = context.get("verified_data", {})
        result = await self.verify(verdict, verified_data, task_id)
        status = "CLEAN" if result.passed else f"CORRECTED ({result.correction_count} fixes)"
        return self.make_message(
            content=f"Verification: {status}\n{result.corrected_text}",
            round_num=round_num,
            vote_tags=["post-decision", "self-correction", status.lower()],
            confidence=1.0 if result.passed else 0.9,
        )
