"""
Nexus AI — Researcher Agent.

Searches the FAISS vector store for relevant context.
Also fetches past emails/conversations for the drafter.

Tool manifest: [vector_search] only.
No external network access. No untrusted content.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)
_settings = get_settings()


class ResearcherAgent(BaseAgent):
    AGENT_ID = "researcher"
    ROLE = "researcher"
    TOOLS = ["vector_search"]

    _SYSTEM = """You are a research agent. You receive a user query and a set of retrieved facts
from a vector knowledge base. Your job is to:
1. Identify which facts are directly relevant to the query
2. Note any important gaps in the retrieved facts
3. Summarise the relevant facts clearly and concisely

STRICT RULES:
- Only use facts from the provided <retrieved_facts> block
- Never invent facts not present in retrieved_facts
- If retrieved_facts is empty, say "No relevant facts found in memory"
- Do not speculate beyond what the facts state

Respond in this format:
RELEVANT FACTS:
<bullet list of relevant facts with source labels>

GAPS:
<what information is missing that would help answer the query>

CONFIDENCE: <0-100>%"""

    def __init__(self, llm: Optional[LLMClient] = None, memory=None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client
        self._memory = memory   # VectorMemory instance, injected

    async def run(
        self,
        task_id: str,
        query: str,
        board: MessageBoard,
        context: dict[str, Any],
        round_num: int = 0,
    ) -> AgentMessage:
        self.use_tool("vector_search")

        # Search vector memory
        facts = await self._search_memory(query)

        # LLM processes the facts
        prompt = (
            f"Query: {query}\n\n"
            f"<retrieved_facts>\n{self._format_facts(facts)}\n</retrieved_facts>"
        )
        response = await self._llm.chat(
            model=_settings.researcher_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.1,
            max_tokens=1024,
        )

        # Extract confidence from response
        confidence = self._parse_confidence(response.content)

        self._log.info(
            "researcher_complete",
            task_id=task_id,
            facts_found=len(facts),
            round=round_num,
        )

        return self.make_message(
            content=response.content,
            round_num=round_num,
            evidence=[{"source": f["source"], "text": f["text"][:100]} for f in facts],
            claims=[f["text"][:80] for f in facts[:3]],
            vote_tags=[
                "source-backed" if facts else "no-memory-match",
                f"{len(facts)}_facts_retrieved",
            ],
            confidence=confidence,
        )

    async def _search_memory(self, query: str, k: int = 5) -> list[dict]:
        """Search vector store. Returns empty list if memory not initialised."""
        if self._memory is None:
            self._log.warning("memory_not_initialised")
            return []
        try:
            return await self._memory.search(query, k=k)
        except Exception as exc:
            self._log.warning("memory_search_failed", error=str(exc))
            return []

    @staticmethod
    def _format_facts(facts: list[dict]) -> str:
        if not facts:
            return "(no facts retrieved)"
        lines = []
        for i, f in enumerate(facts, 1):
            score = f.get("score", 0)
            source = f.get("source", "unknown")
            text = f.get("text", "")
            lines.append(f"[{i}] (score={score:.2f}, source={source}) {text}")
        return "\n".join(lines)

    @staticmethod
    def _parse_confidence(text: str) -> float:
        import re
        m = re.search(r"CONFIDENCE:\s*(\d+)%", text, re.I)
        if m:
            return int(m.group(1)) / 100.0
        return 0.5
