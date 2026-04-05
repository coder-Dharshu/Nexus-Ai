"""
Nexus AI — Agent Meeting Room (with per-agent error isolation).
If one agent fails, it posts an error message and the meeting continues.
"""
from __future__ import annotations
import asyncio, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import structlog
from config.settings import get_settings
from src.agents.base import AgentMessage, MessageBoard
from src.agents.critic import CriticAgent
from src.agents.fact_checker import FactCheckerAgent
from src.agents.reasoner import ReasonerAgent
from src.agents.researcher import ResearcherAgent
from src.agents.synthesizer import SynthesizerAgent
from src.memory.vector_store import VectorMemory

log = structlog.get_logger(__name__)
_s = get_settings()

class MeetingStatus(str, Enum):
    OPEN      = "open"
    RUNNING   = "running"
    CONVERGED = "converged"
    ESCALATED = "escalated"
    FAILED    = "failed"

@dataclass
class MeetingState:
    task_id: str
    query: str
    context: dict[str, Any]
    board: MessageBoard
    status: MeetingStatus = MeetingStatus.OPEN
    current_round: int = 0
    convergence_score: float = 0.0
    unresolved_disputes: list[str] = field(default_factory=list)
    failed_agents: list[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    @property
    def elapsed_s(self) -> float:
        return round((self.end_time or time.time()) - self.start_time, 2)


async def _safe_run(agent, task_id, query, board, context, round_num) -> AgentMessage:
    """Run agent with full error isolation. Returns error message on failure."""
    try:
        return await agent.run(task_id, query, board, context, round_num)
    except Exception as exc:
        log.warning("agent_error_isolated",
                    agent=agent.AGENT_ID, round=round_num, error=str(exc)[:120])
        return AgentMessage(
            agent_id=agent.AGENT_ID, agent_role=agent.ROLE, round_num=round_num,
            content=f"[Agent error — result unavailable: {str(exc)[:80]}]",
            vote_tags=["error", "isolated"], confidence=0.0,
        )


class MeetingRoom:
    def __init__(self, memory: Optional[VectorMemory] = None,
                 researcher=None, reasoner=None, critic=None,
                 fact_checker=None, synthesizer=None) -> None:
        _mem = memory or VectorMemory()
        self._researcher   = researcher   or ResearcherAgent(memory=_mem)
        self._reasoner     = reasoner     or ReasonerAgent()
        self._critic       = critic       or CriticAgent()
        self._fact_checker = fact_checker or FactCheckerAgent()
        self._synthesizer  = synthesizer  or SynthesizerAgent()
        self._memory       = _mem

    async def run(self, state: MeetingState) -> MeetingState:
        state.status = MeetingStatus.RUNNING
        log.info("meeting_start", task_id=state.task_id, query=state.query[:60])
        prev_round_text: Optional[str] = None

        for round_num in range(1, _s.max_debate_rounds + 1):
            state.current_round = round_num
            try:
                await self._run_round(state, round_num)
            except Exception as exc:
                log.error("round_failed", round=round_num, error=str(exc))
                # Don't abort — continue with partial round

            current_text = " ".join(m.content for m in state.board.get_round(round_num))
            if round_num >= 2 and prev_round_text:
                try:
                    sim = await self._memory.similarity(prev_round_text, current_text)
                    state.convergence_score = round(sim, 4)
                    if state.convergence_score >= _s.convergence_threshold:
                        state.status = MeetingStatus.CONVERGED
                        state.end_time = time.time()
                        log.info("meeting_converged", round=round_num, score=state.convergence_score)
                        break
                except Exception:
                    pass  # convergence check failed — continue rounds
            prev_round_text = current_text
        else:
            state.status = MeetingStatus.ESCALATED
            state.end_time = time.time()
            critic_msgs = state.board.get_by_agent("critic")
            if critic_msgs:
                state.unresolved_disputes = [
                    c for c in critic_msgs[-1].challenges
                    if "no substantive" not in c.lower()
                ][:5]

        return state

    async def _run_round(self, state: MeetingState, round_num: int) -> None:
        if round_num == 1:
            # Researcher first (reasoner reads researcher output)
            r = await _safe_run(self._researcher, state.task_id, state.query,
                                 state.board, state.context, round_num)
            state.board.post(r)
            rz = await _safe_run(self._reasoner, state.task_id, state.query,
                                  state.board, state.context, round_num)
            state.board.post(rz)
            # Critic and fact-checker in parallel
            results = await asyncio.gather(
                _safe_run(self._critic, state.task_id, state.query,
                          state.board, state.context, round_num),
                _safe_run(self._fact_checker, state.task_id, state.query,
                          state.board, state.context, round_num),
                return_exceptions=False,
            )
            for msg in results:
                if isinstance(msg, AgentMessage):
                    state.board.post(msg)
        else:
            # All agents in parallel for later rounds
            results = await asyncio.gather(
                *[_safe_run(a, state.task_id, state.query,
                            state.board, state.context, round_num)
                  for a in [self._researcher, self._reasoner,
                             self._critic, self._fact_checker]],
                return_exceptions=False,
            )
            for msg in results:
                if isinstance(msg, AgentMessage):
                    state.board.post(msg)

    async def synthesize(self, state: MeetingState) -> AgentMessage:
        return await _safe_run(
            self._synthesizer, state.task_id, state.query,
            state.board, state.context, state.current_round,
        )
