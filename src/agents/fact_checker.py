"""
Nexus AI — Fact Checker Agent.

Verifies key claims against:
  - Historical baseline cache (SQLite)
  - Statistical plausibility checks
  - Cross-reference against verified browser data

Runs in parallel with the Critic during the meeting room.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)
_settings = get_settings()


class FactCheckerAgent(BaseAgent):
    AGENT_ID = "fact_checker"
    ROLE = "fact_checker"
    TOOLS: list[str] = []

    _SYSTEM = """You are a fact-checking agent. You receive agent outputs and verified source data.
Your job is to verify key factual claims:
1. Check numerical values against historical baselines provided
2. Flag any value that deviates >15% from the baseline as suspicious
3. Confirm or deny specific factual claims made by other agents
4. Note if today's value is within normal volatility range

Baseline data will be provided in <baselines> if available.

Format:
VERIFIED CLAIMS:
<list of claims confirmed with evidence>

SUSPICIOUS CLAIMS:
<list of claims that deviate from baseline or lack evidence>

BASELINE COMPARISON:
<today vs historical, deviation %, within normal range?>

STATUS: <CONFIRMED | PARTIALLY_CONFIRMED | UNVERIFIABLE>
CONFIDENCE: <0-100>%"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def run(
        self,
        task_id: str,
        query: str,
        board: MessageBoard,
        context: dict[str, Any],
        round_num: int = 0,
    ) -> AgentMessage:
        verified_data = context.get("verified_data", "")
        baselines = await self._fetch_baselines(context)
        board_text = self._format_board(board.get_all())

        prompt = (
            f"Query: {query}\n\n"
            f"<verified_data>\n{verified_data}\n</verified_data>\n\n"
            f"<baselines>\n{baselines}\n</baselines>\n\n"
            f"<agent_outputs>\n{board_text}\n</agent_outputs>"
        )

        response = await self._llm.chat(
            model=_settings.decision_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.1,
            max_tokens=800,
        )

        status = self._extract_status(response.content)
        confidence = self._parse_confidence(response.content)

        self._log.info("fact_checker_complete", task_id=task_id, round=round_num, status=status)

        return self.make_message(
            content=response.content,
            round_num=round_num,
            vote_tags=[status.lower(), "baseline-checked", f"round-{round_num}"],
            confidence=confidence,
        )

    async def _fetch_baselines(self, context: dict) -> str:
        """Fetch historical baseline values from cache."""
        query_type = context.get("query_category", "unknown")
        cached = context.get("baseline_cache", {})
        if not cached:
            return "(no historical baselines available — first run)"
        lines = [f"{k}: {v}" for k, v in cached.items()]
        return "\n".join(lines)

    @staticmethod
    def _format_board(messages: list) -> str:
        if not messages:
            return "(no prior messages)"
        return "\n\n".join(
            f"[{m.agent_role.upper()}] {m.content[:400]}"
            for m in messages
        )

    @staticmethod
    def _extract_status(text: str) -> str:
        import re
        m = re.search(r"STATUS:\s*(CONFIRMED|PARTIALLY_CONFIRMED|UNVERIFIABLE)", text, re.I)
        return m.group(1).upper() if m else "UNVERIFIABLE"

    @staticmethod
    def _parse_confidence(text: str) -> float:
        import re
        m = re.search(r"CONFIDENCE:\s*(\d+)%", text, re.I)
        return int(m.group(1)) / 100.0 if m else 0.5
