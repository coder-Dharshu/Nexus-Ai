"""
Nexus AI — Critic Agent.

Adversarial by design. Reads every other agent's output and specifically hunts for:
  - Unsupported claims (not backed by scraped or memory data)
  - Logical gaps or non-sequiturs
  - Contradictions with the verified source data
  - Overconfident statements
  - Missing alternative explanations

Uses DeepSeek-R1-Distill-32B — chosen for lowest hallucination rate.
Runs max MAX_DEBATE_ROUNDS times.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)
_settings = get_settings()


class CriticAgent(BaseAgent):
    AGENT_ID = "critic"
    ROLE = "critic"
    TOOLS: list[str] = []  # No tools — reads board only

    _SYSTEM = """You are an adversarial critic in a multi-agent reasoning system.
Your ONLY job is to find weaknesses in other agents' reasoning.

You must challenge:
1. Any claim not directly supported by the provided verified_data or cited facts
2. Logical leaps or unsupported inferences
3. Missing alternative explanations
4. Overconfident conclusions
5. Contradictions between agents

For each challenge:
- Name the agent you are challenging
- Quote the specific claim
- Explain why it is weak or unsupported
- Suggest what evidence would be needed

If reasoning is sound and well-supported, say "No substantive challenges" and briefly explain why.

Format:
CHALLENGES:
[Agent] "quoted claim" → Weakness: ...

WITHDRAWN CHALLENGES: (list any previous challenges you now accept)

STATUS: <DISPUTED | CONVERGING | CONVERGED>
CONFIDENCE: <0-100>%"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client
        self._challenge_history: list[str] = []

    async def run(
        self,
        task_id: str,
        query: str,
        board: MessageBoard,
        context: dict[str, Any],
        round_num: int = 0,
    ) -> AgentMessage:
        # Hard stop — critic cannot run more than max_debate_rounds
        if round_num > _settings.max_debate_rounds:
            self._log.warning("critic_max_rounds_reached", task_id=task_id)
            return self.make_message(
                content="Max debate rounds reached. Critic standing down.",
                round_num=round_num,
                vote_tags=["max_rounds", "standing_down"],
                confidence=1.0,
            )

        all_messages = board.get_all()
        board_summary = self._format_board(all_messages, exclude_agent="critic")
        verified_data = context.get("verified_data", "(no live data in this pipeline)")
        prev_challenges = "\n".join(self._challenge_history[-3:]) if self._challenge_history else "none"

        prompt = (
            f"Query: {query}\n\n"
            f"<verified_data>\n{verified_data}\n</verified_data>\n\n"
            f"<agent_outputs>\n{board_summary}\n</agent_outputs>\n\n"
            f"Previous challenges raised: {prev_challenges}"
        )

        response = await self._llm.chat(
            model=_settings.critic_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.15,
            max_tokens=1200,
        )

        # Track challenge history for context in later rounds
        self._challenge_history.append(f"Round {round_num}: {response.content[:200]}")

        challenges = self._extract_challenges(response.content)
        withdrawn = self._extract_withdrawn(response.content)
        status = self._extract_status(response.content)
        confidence = self._parse_confidence(response.content)

        tags = [f"round-{round_num}", status.lower()]
        if withdrawn:
            tags.append(f"withdrew-{len(withdrawn)}")
        if not challenges or challenges == ["No substantive challenges"]:
            tags.append("no-new-challenges")

        self._log.info(
            "critic_complete",
            task_id=task_id,
            round=round_num,
            challenges=len(challenges),
            withdrawn=len(withdrawn),
            status=status,
        )

        return self.make_message(
            content=response.content,
            round_num=round_num,
            challenges=challenges,
            vote_tags=tags,
            confidence=confidence,
        )

    @staticmethod
    def _format_board(messages: list, exclude_agent: str = "") -> str:
        if not messages:
            return "(no prior messages)"
        lines = []
        for m in messages:
            if m.agent_id == exclude_agent:
                continue
            lines.append(f"[{m.agent_role.upper()} Round {m.round_num}]")
            lines.append(m.content[:600])
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _extract_challenges(text: str) -> list[str]:
        import re
        section = re.search(r"CHALLENGES:\s*(.*?)(?:WITHDRAWN|STATUS:|CONFIDENCE:|$)", text, re.S | re.I)
        if not section:
            return []
        raw = section.group(1).strip()
        if not raw or "no substantive" in raw.lower():
            return ["No substantive challenges"]
        return [line.strip() for line in raw.split("\n") if line.strip()][:5]

    @staticmethod
    def _extract_withdrawn(text: str) -> list[str]:
        import re
        section = re.search(r"WITHDRAWN CHALLENGES:\s*(.*?)(?:STATUS:|CONFIDENCE:|$)", text, re.S | re.I)
        if not section:
            return []
        raw = section.group(1).strip()
        return [line.strip() for line in raw.split("\n") if line.strip() and "none" not in line.lower()]

    @staticmethod
    def _extract_status(text: str) -> str:
        import re
        m = re.search(r"STATUS:\s*(DISPUTED|CONVERGING|CONVERGED)", text, re.I)
        return m.group(1).upper() if m else "DISPUTED"

    @staticmethod
    def _parse_confidence(text: str) -> float:
        import re
        m = re.search(r"CONFIDENCE:\s*(\d+)%", text, re.I)
        return int(m.group(1)) / 100.0 if m else 0.5
