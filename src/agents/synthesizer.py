"""
Nexus AI — Synthesizer Agent.

The only agent whose output the user ever sees directly.
Takes the post-debate consensus and formats it into a clean, cited,
confidence-scored final answer.

Every number in the output must have a source citation.
Model used is always disclosed. HITL gate is triggered if action needed.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)
_settings = get_settings()


class SynthesizerAgent(BaseAgent):
    AGENT_ID = "synthesizer"
    ROLE = "synthesizer"
    TOOLS = ["hitl_trigger"]

    _SYSTEM = """You are the synthesizer — the final formatting agent.
You receive the full output of a multi-agent debate and produce the user-facing answer.

RULES:
1. Every number, price, date, or statistic MUST have a source citation in brackets [source]
2. If a value has no source, do not include it in the answer
3. State the confidence level explicitly
4. Disclose how many sources agreed
5. Flag any remaining uncertainties the agents identified
6. Keep the answer concise and actionable

Format:
ANSWER:
<clean, user-friendly answer with [source] citations on every fact>

CONFIDENCE: <percentage> (<N>/<M> sources agree)
SOURCES: <list of source URLs or names>
UNCERTAINTIES: <anything agents could not resolve — or "none">
MODEL: <model used for synthesis>"""

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
        self.use_tool("hitl_trigger")   # assert permission exists

        transcript = board.full_transcript()
        verified_data = context.get("verified_data", "")
        sources = context.get("sources", [])
        overall_conf = context.get("confidence", 0.0)

        prompt = (
            f"Query: {query}\n\n"
            f"<verified_data>\n{verified_data}\n</verified_data>\n\n"
            f"<debate_transcript>\n{transcript}\n</debate_transcript>\n\n"
            f"Overall cross-source confidence: {overall_conf:.0%}\n"
            f"Sources: {', '.join(sources) if sources else 'none'}"
        )

        response = await self._llm.chat(
            model=_settings.orchestrator_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.1,
            max_tokens=1000,
        )

        # Validate no number is uncited
        answer_text = response.content
        uncited = self._check_uncited_numbers(answer_text)
        if uncited:
            self._log.warning("synthesizer_uncited_numbers", count=len(uncited), task_id=task_id)
            # Append warning if uncited numbers found
            answer_text += f"\n\n⚠ Validation note: {len(uncited)} value(s) may lack explicit source citation."

        self._log.info("synthesizer_complete", task_id=task_id, sources=len(sources))

        return self.make_message(
            content=answer_text,
            round_num=round_num,
            vote_tags=["final_answer", f"conf={overall_conf:.0%}", f"{len(sources)}_sources"],
            confidence=overall_conf,
        )

    @staticmethod
    def _check_uncited_numbers(text: str) -> list[str]:
        """Find numeric values in ANSWER section that lack a [source] citation."""
        import re
        answer_section = re.search(r"ANSWER:\s*(.*?)(?:CONFIDENCE:|SOURCES:|$)", text, re.S | re.I)
        if not answer_section:
            return []
        answer = answer_section.group(1)
        # Find numbers (prices, percentages, counts)
        numbers = re.findall(r"(?:₹|£|\$|€)?\s*[\d,]+\.?\d*\s*(?:%|per\s+\w+)?", answer)
        # Check each has a nearby [citation]
        uncited = []
        for num in numbers:
            idx = answer.find(num)
            nearby = answer[max(0, idx-20):idx+len(num)+30]
            if "[" not in nearby:
                uncited.append(num)
        return uncited
