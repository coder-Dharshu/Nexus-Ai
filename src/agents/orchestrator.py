"""
Nexus AI — Orchestrator Agent.

The master controller. Nothing runs without the orchestrator's assignment.

Responsibilities:
  1. Receive classified query
  2. Decompose into subtasks
  3. Assign each subtask to the right agent
  4. Track completion
  5. Decide when to enter the meeting room
  6. Decide when convergence has been reached
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, MessageBoard
from src.agents.classifier import ClassificationResult, QueryType
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)
_settings = get_settings()


class TaskStatus(str, Enum):
    PENDING     = "pending"
    RUNNING     = "running"
    COMPLETED   = "completed"
    FAILED      = "failed"
    BLOCKED     = "blocked"   # waiting on HITL


@dataclass
class Subtask:
    id: str
    agent_id: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[Any] = None


@dataclass
class OrchestratorPlan:
    task_id: str
    original_query: str
    classification: ClassificationResult
    subtasks: list[Subtask]
    pipeline: str
    notes: str = ""

    def all_completed(self) -> bool:
        return all(s.status == TaskStatus.COMPLETED for s in self.subtasks)

    def next_runnable(self) -> list[Subtask]:
        """Return subtasks whose dependencies are all completed."""
        completed_ids = {s.id for s in self.subtasks if s.status == TaskStatus.COMPLETED}
        return [
            s for s in self.subtasks
            if s.status == TaskStatus.PENDING
            and all(dep in completed_ids for dep in s.depends_on)
        ]


class OrchestratorAgent(BaseAgent):
    AGENT_ID = "orchestrator"
    ROLE = "orchestrator"
    TOOLS = ["task_assign", "state_write"]   # internal tools only

    _DECOMPOSE_SYSTEM = """You are the orchestrator of a multi-agent AI system.
Given a classified query, produce a JSON execution plan.

Agents available:
  - researcher:    searches vector memory for facts and past context
  - reasoner:      pure chain-of-thought analysis, no tools
  - critic:        challenges claims and finds logical gaps
  - fact_checker:  verifies against historical baselines
  - drafter:       writes tone-matched emails/messages (action tasks only)
  - synthesizer:   formats final answer with citations

Return ONLY valid JSON:
{
  "subtasks": [
    {"id": "t1", "agent_id": "researcher", "description": "...", "depends_on": []},
    {"id": "t2", "agent_id": "reasoner",   "description": "...", "depends_on": ["t1"]}
  ],
  "pipeline": "short description",
  "notes": "any special handling"
}"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def plan(
        self,
        task_id: str,
        query: str,
        classification: ClassificationResult,
    ) -> OrchestratorPlan:
        """
        Build an execution plan for the given query.
        For simple queries, uses pre-built templates (faster, no LLM call).
        For complex queries, calls the orchestrator LLM.
        """
        # Use template plans for well-understood patterns
        template = self._template_plan(task_id, query, classification)
        if template:
            log.info("orchestrator_template_plan", task_id=task_id, type=classification.query_type)
            return template

        # Fall back to LLM decomposition
        return await self._llm_plan(task_id, query, classification)

    def _template_plan(
        self,
        task_id: str,
        query: str,
        cls: ClassificationResult,
    ) -> Optional[OrchestratorPlan]:
        """Pre-built templates for common query patterns."""

        if cls.query_type == QueryType.LIVE_DATA:
            return OrchestratorPlan(
                task_id=task_id,
                original_query=query,
                classification=cls,
                subtasks=[
                    Subtask("t1", "browser_fleet",  "Scrape 6 sources in parallel",       []),
                    Subtask("t2", "validator",      "Validate all scraped outputs",         ["t1"]),
                    Subtask("t3", "cross_verifier", "Compute weighted consensus",           ["t2"]),
                    Subtask("t4", "researcher",     "Add memory context to verified data",  ["t3"]),
                    Subtask("t5", "reasoner",       "Contextual analysis of verified data", ["t4"]),
                    Subtask("t6", "critic",         "Challenge reasoning",                  ["t5"]),
                    Subtask("t7", "fact_checker",   "Historical baseline check",            ["t5"]),
                    Subtask("t8", "synthesizer",    "Format final cited answer",            ["t6", "t7"]),
                ],
                pipeline="6× browser → validate → cross-verify → meeting → decision",
            )

        if cls.query_type == QueryType.ACTION:
            return OrchestratorPlan(
                task_id=task_id,
                original_query=query,
                classification=cls,
                subtasks=[
                    Subtask("t1", "researcher",  "Fetch past emails/context for tone matching", []),
                    Subtask("t2", "drafter",     "Compose tone-matched draft",                  ["t1"]),
                    Subtask("t3", "hitl_gate",   "Send draft to user for approval",             ["t2"]),
                    Subtask("t4", "executor",    "Execute approved action",                     ["t3"]),
                ],
                pipeline="researcher → drafter → HITL gate → executor",
            )

        if cls.query_type == QueryType.KNOWLEDGE:
            return OrchestratorPlan(
                task_id=task_id,
                original_query=query,
                classification=cls,
                subtasks=[
                    Subtask("t1", "researcher",   "Search memory and knowledge base",  []),
                    Subtask("t2", "reasoner",     "Analyse facts and draw conclusions",["t1"]),
                    Subtask("t3", "critic",       "Challenge reasoning gaps",          ["t2"]),
                    Subtask("t4", "fact_checker", "Verify key claims",                 ["t2"]),
                    Subtask("t5", "synthesizer",  "Format final answer with sources",  ["t3", "t4"]),
                ],
                pipeline="researcher → reasoner → critic → fact_checker → synthesizer",
            )

        return None

    async def _llm_plan(
        self,
        task_id: str,
        query: str,
        cls: ClassificationResult,
    ) -> OrchestratorPlan:
        """LLM-generated plan for complex or ambiguous queries."""
        prompt = (
            f"Query type: {cls.query_type.value}\n"
            f"Confidence: {cls.confidence:.0%}\n"
            f"Entities: {cls.entities}\n"
            f"Query: {query}"
        )
        response = await self._llm.chat(
            model=_settings.orchestrator_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._DECOMPOSE_SYSTEM,
            temperature=0.1,
            max_tokens=512,
            json_mode=True,
        )
        try:
            data = json.loads(response.content)
            subtasks = [
                Subtask(
                    id=s["id"],
                    agent_id=s["agent_id"],
                    description=s["description"],
                    depends_on=s.get("depends_on", []),
                )
                for s in data.get("subtasks", [])
            ]
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("orchestrator_parse_failed", error=str(exc))
            # Fallback to knowledge plan
            return self._template_plan(task_id, query, ClassificationResult(
                query_type=QueryType.KNOWLEDGE,
                confidence=0.5,
                entities=cls.entities,
                method="fallback",
                pipeline="knowledge fallback",
            )) or OrchestratorPlan(
                task_id=task_id,
                original_query=query,
                classification=cls,
                subtasks=[Subtask("t1", "reasoner", query, [])],
                pipeline="direct reasoner",
            )

        return OrchestratorPlan(
            task_id=task_id,
            original_query=query,
            classification=cls,
            subtasks=subtasks,
            pipeline=data.get("pipeline", "llm-generated"),
            notes=data.get("notes", ""),
        )

    async def run(self, task_id, query, board, context, round_num=0):
        cls = context.get("classification")
        plan = await self.plan(task_id, query, cls)
        summary = f"Plan: {plan.pipeline}\nSubtasks: {len(plan.subtasks)}"
        for s in plan.subtasks:
            summary += f"\n  {s.id} → {s.agent_id}: {s.description}"
        return self.make_message(
            content=summary,
            round_num=round_num,
            vote_tags=["plan_ready", f"{len(plan.subtasks)}_subtasks"],
            confidence=1.0,
        )
