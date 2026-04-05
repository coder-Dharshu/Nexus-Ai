"""
Nexus AI — Reasoner Agent.

Pure chain-of-thought reasoning. No tools. No external access.
Receives facts from Researcher and verified data from browser agents.
Draws conclusions ONLY from what was provided — never from training memory.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)
_settings = get_settings()


class ReasonerAgent(BaseAgent):
    AGENT_ID = "reasoner"
    ROLE = "reasoner"
    TOOLS: list[str] = []  # Intentionally empty — pure reasoning

    _SYSTEM = """You are a reasoning agent in a multi-agent system.
You receive verified facts and data from other agents.
Your job is to analyse these facts and draw well-reasoned conclusions.

ABSOLUTE RULES:
- You may ONLY reason from facts provided in the <verified_data> block
- You may NEVER use your training knowledge to invent prices, dates, statistics, or facts
- If a fact is missing, state "Insufficient data to conclude X"
- Every claim must cite which fact it is derived from
- Be explicit about your reasoning chain

Format:
ANALYSIS:
<step-by-step reasoning with fact citations>

CONCLUSIONS:
<numbered list of conclusions with confidence>

UNCERTAINTIES:
<what cannot be concluded from available data>

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
        # Gather all previous messages as verified data
        prior_messages = board.get_all()
        verified_data = self._compile_verified_data(prior_messages, context)

        prompt = (
            f"Query: {query}\n\n"
            f"<verified_data>\n{verified_data}\n</verified_data>"
        )

        response = await self._llm.chat(
            model=_settings.orchestrator_model,  # uses Qwen3 for reasoning
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.2,
            max_tokens=1500,
        )

        confidence = self._parse_confidence(response.content)

        self._log.info("reasoner_complete", task_id=task_id, round=round_num)

        return self.make_message(
            content=response.content,
            round_num=round_num,
            claims=self._extract_conclusions(response.content),
            vote_tags=["chain-of-thought", "no-tools", f"round-{round_num}"],
            confidence=confidence,
        )

    def _compile_verified_data(
        self, prior_messages: list, context: dict
    ) -> str:
        """Compile all prior agent outputs into a clean context block."""
        parts = []

        # Verified browser data (from cross-verifier)
        if "verified_data" in context:
            vd = context["verified_data"]
            parts.append(f"VERIFIED LIVE DATA:\n{vd}")

        # Researcher facts
        researcher_msgs = [m for m in prior_messages if m.agent_id == "researcher"]
        if researcher_msgs:
            latest = researcher_msgs[-1]
            parts.append(f"RESEARCHER FACTS:\n{latest.content}")

        if not parts:
            return "(no prior verified data available)"

        return "\n\n".join(parts)

    @staticmethod
    def _parse_confidence(text: str) -> float:
        import re
        m = re.search(r"CONFIDENCE:\s*(\d+)%", text, re.I)
        return int(m.group(1)) / 100.0 if m else 0.5

    @staticmethod
    def _extract_conclusions(text: str) -> list[str]:
        import re
        section = re.search(r"CONCLUSIONS:\s*(.*?)(?:UNCERTAINTIES:|CONFIDENCE:|$)", text, re.S | re.I)
        if not section:
            return []
        lines = section.group(1).strip().split("\n")
        return [l.strip().lstrip("0123456789.-) ") for l in lines if l.strip()][:5]
