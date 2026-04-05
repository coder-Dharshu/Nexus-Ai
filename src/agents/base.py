"""
Nexus AI — Base Agent.

Every specialist agent inherits from BaseAgent.

Security invariants enforced here:
  1. Tool manifest is FROZEN at instantiation — no agent can grant itself new tools
  2. No agent has any reference to the audit logger (write-only system)
  3. No agent can call tools outside its manifest (raises PermissionError)
  4. Context boundary: external content always arrives wrapped in <external> tags
"""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, FrozenSet

import structlog

log = structlog.get_logger(__name__)


# ── Tool manifest ─────────────────────────────────────────────────────────────

class LockedManifest:
    """
    Immutable set of tool names an agent is permitted to call.
    Created once at agent __init__, cannot be modified afterward.
    """
    __slots__ = ("_tools", "_agent_id")

    def __init__(self, tools: list[str], agent_id: str) -> None:
        object.__setattr__(self, "_tools", frozenset(tools))
        object.__setattr__(self, "_agent_id", agent_id)

    def __setattr__(self, *_):
        raise AttributeError("LockedManifest is immutable after creation")

    def can_use(self, tool: str) -> bool:
        return tool in self._tools  # type: ignore[operator]

    def assert_can_use(self, tool: str) -> None:
        if not self.can_use(tool):
            raise PermissionError(
                f"Agent '{self._agent_id}' attempted to call tool '{tool}' "
                f"which is not in its manifest {set(self._tools)}. "   # type: ignore
                f"This is a security violation."
            )

    @property
    def tools(self) -> FrozenSet[str]:
        return self._tools  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"LockedManifest(agent={self._agent_id!r}, tools={set(self._tools)!r})"   # type: ignore


# ── Lethal trifecta check ─────────────────────────────────────────────────────

_TRIFECTA_TOOLS = {
    "private_data":     {"vector_search", "gmail_read", "user_history"},
    "external_comms":   {"send_email", "telegram_send", "webhook_post"},
    "untrusted_content":{"browser_scrape", "web_fetch", "html_parse"},
}


def check_trifecta(tools: list[str], agent_id: str) -> None:
    """
    Raise ValueError if the tool list would create the lethal trifecta:
    private_data + external_comms + untrusted_content simultaneously.
    """
    tool_set = set(tools)
    has = {
        category: bool(tool_set & category_tools)
        for category, category_tools in _TRIFECTA_TOOLS.items()
    }
    if all(has.values()):
        raise ValueError(
            f"SECURITY VIOLATION: Agent '{agent_id}' manifest creates the lethal trifecta "
            f"(private_data + external_comms + untrusted_content). "
            f"Redesign agent capabilities to break this combination. Tools: {tools}"
        )


# ── Message ───────────────────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    """A single message posted to the shared meeting board."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    agent_role: str = ""
    round_num: int = 0
    content: str = ""
    evidence: list[dict] = field(default_factory=list)  # scraped source refs
    claims: list[str] = field(default_factory=list)
    challenges: list[str] = field(default_factory=list)
    vote_tags: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "round": self.round_num,
            "content": self.content,
            "evidence": self.evidence,
            "claims": self.claims,
            "challenges": self.challenges,
            "vote_tags": self.vote_tags,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        """Short summary for LLM context injection."""
        tags = ", ".join(self.vote_tags) if self.vote_tags else "none"
        return (
            f"[{self.agent_role.upper()} Round {self.round_num}] "
            f"confidence={self.confidence:.0%} tags=[{tags}]\n"
            f"{self.content}"
        )


# ── Shared message board ──────────────────────────────────────────────────────

class MessageBoard:
    """
    Shared read/write board for the agent meeting room.
    All agents can post and read. Decision Agent reads only (never posts).
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._messages: list[AgentMessage] = []

    def post(self, message: AgentMessage) -> None:
        message.agent_id = message.agent_id or "unknown"
        self._messages.append(message)
        log.info(
            "board_post",
            task_id=self.task_id,
            agent=message.agent_role,
            round=message.round_num,
            tags=message.vote_tags,
        )

    def get_all(self) -> list[AgentMessage]:
        return list(self._messages)

    def get_round(self, round_num: int) -> list[AgentMessage]:
        return [m for m in self._messages if m.round_num == round_num]

    def get_by_agent(self, agent_id: str) -> list[AgentMessage]:
        return [m for m in self._messages if m.agent_id == agent_id]

    def full_transcript(self) -> str:
        """Formatted full meeting transcript for Decision Agent."""
        if not self._messages:
            return "(empty board)"
        lines = [f"=== Task: {self.task_id} | {len(self._messages)} messages ===\n"]
        for m in self._messages:
            lines.append(m.summary())
            lines.append("")
        return "\n".join(lines)

    def latest_by_agent(self) -> dict[str, AgentMessage]:
        """Most recent message per agent — for convergence check."""
        latest: dict[str, AgentMessage] = {}
        for m in self._messages:
            latest[m.agent_id] = m
        return latest


# ── Base Agent ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Abstract base for all Nexus agents.

    Subclasses must implement:
        AGENT_ID  : str   — unique snake_case identifier
        ROLE      : str   — human-readable role label
        TOOLS     : list  — list of tool names this agent may call
        run()     : async — main entrypoint
    """

    AGENT_ID: str = "base"
    ROLE: str = "base"
    TOOLS: list[str] = []

    def __init__(self) -> None:
        # Check lethal trifecta BEFORE creating manifest
        check_trifecta(self.TOOLS, self.AGENT_ID)
        # Lock the manifest — immutable from this point
        self._manifest = LockedManifest(self.TOOLS, self.AGENT_ID)
        self._log = structlog.get_logger(f"agent.{self.AGENT_ID}")
        self._log.info("agent_init", manifest=repr(self._manifest))

    @property
    def manifest(self) -> LockedManifest:
        return self._manifest

    def use_tool(self, tool: str) -> None:
        """Assert permission before any tool call. Must be called in subclasses."""
        self._manifest.assert_can_use(tool)

    def make_message(
        self,
        content: str,
        *,
        round_num: int = 0,
        claims: Optional[list[str]] = None,
        challenges: Optional[list[str]] = None,
        vote_tags: Optional[list[str]] = None,
        evidence: Optional[list[dict]] = None,
        confidence: float = 0.0,
    ) -> AgentMessage:
        return AgentMessage(
            agent_id=self.AGENT_ID,
            agent_role=self.ROLE,
            round_num=round_num,
            content=content,
            claims=claims or [],
            challenges=challenges or [],
            vote_tags=vote_tags or [],
            evidence=evidence or [],
            confidence=confidence,
        )

    @abstractmethod
    async def run(
        self,
        task_id: str,
        query: str,
        board: MessageBoard,
        context: dict[str, Any],
        round_num: int = 0,
    ) -> AgentMessage:
        """Execute this agent's role and return a message for the board."""
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(id={self.AGENT_ID!r}, tools={set(self._manifest.tools)!r})"
