"""
Nexus AI — Decision Agent.

The Decision Agent is the final authority. It:
  1. Reads the FULL meeting transcript (all rounds, all agents)
  2. Scores each agent's contribution by evidence quality
  3. Identifies consensus points vs genuine disputes
  4. Cross-checks every claim against verified browser data
  5. Produces the authoritative final verdict with justification

Rules:
  - Never posts during the meeting — reads only
  - No tools — analysis only
  - Browser-verified data always overrules LLM reasoning
  - Disputed points that did not resolve are flagged as uncertain
  - Every number in the verdict must trace to a scraped source
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard

log = structlog.get_logger(__name__)
_settings = get_settings()


# ── Agent scoring ─────────────────────────────────────────────────────────────

@dataclass
class AgentScore:
    agent_id: str
    role: str
    evidence_score: float     # claims backed by scraped data
    stability_score: float    # claims not withdrawn under pressure
    challenge_score: float    # successful challenges raised (critic only)
    overall: float
    notes: str = ""


@dataclass
class DecisionVerdict:
    answer: str
    confidence: float
    sources: list[str]
    accepted_agents: list[str]        # whose arguments were accepted
    discarded_agents: list[str]       # whose arguments were discarded
    resolved_disputes: list[str]
    unresolved_disputes: list[str]    # flagged as uncertain
    agent_scores: list[AgentScore]
    model_used: str
    rounds_taken: int
    verified_data_used: bool          # True if browser data was available


class DecisionAgent(BaseAgent):
    """
    Reads the full meeting transcript and produces the final verdict.
    Never posts during the meeting. No tools. Pure analysis.
    """

    AGENT_ID = "decision"
    ROLE = "decision"
    TOOLS: list[str] = []   # No tools — reads only

    _SCORE_SYSTEM = """You are the Decision Agent in a multi-agent reasoning system.
You receive the full transcript of an internal agent debate and verified browser data.

Your task:
1. Score each agent's contribution (0-100) based on:
   - Evidence quality: were claims backed by scraped source data?
   - Stability: did the agent maintain its position under valid challenge?
   - Accuracy: were claims consistent with verified_data?

2. Identify which claims were accepted by consensus vs disputed

3. Cross-check: any claim contradicting verified_data is automatically wrong

4. List unresolved disputes that must be flagged as uncertain in the final answer

Respond ONLY with valid JSON:
{
  "agent_scores": [
    {"agent_id": "...", "role": "...", "evidence_score": 0-100,
     "stability_score": 0-100, "challenge_score": 0-100, "overall": 0-100, "notes": "..."}
  ],
  "accepted_claims": ["..."],
  "disputed_claims": ["..."],
  "resolved_disputes": ["..."],
  "unresolved_disputes": ["..."]
}"""

    _VERDICT_SYSTEM = """You are the Decision Agent. You have scored all agents and identified consensus.
Now produce the final authoritative verdict.

RULES:
1. Every number, price, date, or statistic MUST be from verified_data or accepted claims
2. State explicitly which agents' reasoning was accepted and which was discarded
3. Flag all unresolved disputes as "uncertain"
4. State overall confidence as a percentage
5. Be concise — this is the user-facing answer

Format:
VERDICT:
<answer with [source] citations on every fact>

ACCEPTED: <which agents' arguments were used>
DISCARDED: <which arguments were rejected and why>
UNCERTAIN: <unresolved disputes — or "none">
CONFIDENCE: <percentage> (<N>/<M> sources verified)
MODEL: {model}"""

    def __init__(self, llm=None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    # ── Main entrypoint ────────────────────────────────────────────────────────

    async def decide(
        self,
        task_id: str,
        query: str,
        board: MessageBoard,
        context: dict[str, Any],
        rounds_taken: int,
    ) -> DecisionVerdict:
        """
        Read the full transcript and produce a final verdict.
        This is the only method external code calls.
        """
        transcript = board.full_transcript()
        verified_data = context.get("verified_data", "")
        sources = context.get("sources", [])
        overall_conf = context.get("confidence", 0.0)

        self._log.info("decision_start", task_id=task_id, rounds=rounds_taken,
                       transcript_len=len(transcript))

        # Step 1 — Score agents and identify consensus
        import json
        score_prompt = (
            f"Query: {query}\n\n"
            f"<verified_data>\n{verified_data or '(no browser data)'}\n</verified_data>\n\n"
            f"<full_transcript>\n{transcript}\n</full_transcript>"
        )
        score_resp = await self._llm.chat(
            model=_settings.decision_model,
            messages=[{"role": "user", "content": score_prompt}],
            system=self._SCORE_SYSTEM,
            temperature=0.0,
            max_tokens=1500,
            json_mode=True,
        )

        try:
            score_data = json.loads(score_resp.content)
        except json.JSONDecodeError:
            score_data = {}

        agent_scores = self._parse_agent_scores(score_data)
        accepted_claims    = score_data.get("accepted_claims", [])
        resolved_disputes  = score_data.get("resolved_disputes", [])
        unresolved_disputes = score_data.get("unresolved_disputes", [])

        # Step 2 — Produce the final verdict
        verdict_system = self._VERDICT_SYSTEM.format(model=_settings.decision_model)
        verdict_prompt = (
            f"Query: {query}\n\n"
            f"<verified_data>\n{verified_data or '(no browser data)'}\n</verified_data>\n\n"
            f"Accepted claims:\n" + "\n".join(f"- {c}" for c in accepted_claims) + "\n\n"
            f"Unresolved disputes:\n" + "\n".join(f"- {d}" for d in unresolved_disputes) + "\n\n"
            f"Agent scores:\n" + "\n".join(
                f"- {s.agent_id}: {s.overall:.0f}% ({s.notes})" for s in agent_scores
            ) + "\n\n"
            f"Sources: {', '.join(sources) if sources else 'no browser sources'}\n"
            f"Overall cross-source confidence: {overall_conf:.0%}"
        )

        verdict_resp = await self._llm.chat(
            model=_settings.decision_model,
            messages=[{"role": "user", "content": verdict_prompt}],
            system=verdict_system,
            temperature=0.1,
            max_tokens=1200,
        )

        # Step 3 — Parse verdict
        answer     = self._extract_section(verdict_resp.content, "VERDICT")
        accepted   = self._extract_list(verdict_resp.content, "ACCEPTED")
        discarded  = self._extract_list(verdict_resp.content, "DISCARDED")
        uncertain  = self._extract_list(verdict_resp.content, "UNCERTAIN")
        confidence = self._extract_confidence(verdict_resp.content) or overall_conf

        # Step 4 — Final cross-check: ensure no invented numbers
        if verified_data and answer:
            answer = self._ground_check(answer, verified_data)

        self._log.info(
            "decision_complete",
            task_id=task_id,
            confidence=confidence,
            accepted_agents=accepted,
            unresolved=len(unresolved_disputes),
        )

        return DecisionVerdict(
            answer=answer or verdict_resp.content,
            confidence=confidence,
            sources=sources,
            accepted_agents=accepted,
            discarded_agents=discarded,
            resolved_disputes=resolved_disputes,
            unresolved_disputes=uncertain or unresolved_disputes,
            agent_scores=agent_scores,
            model_used=_settings.decision_model,
            rounds_taken=rounds_taken,
            verified_data_used=bool(verified_data),
        )

    async def run(self, task_id, query, board, context, round_num=0):
        """
        BaseAgent interface. Decision Agent reads transcript and produces verdict.
        Called after the meeting ends — NOT during rounds.
        """
        try:
            verdict = await self.decide(
                task_id=task_id, query=query, board=board,
                context=context, rounds_taken=context.get("rounds_taken", board.get_all().__len__()),
            )
            return self.make_message(
                content=f"VERDICT:\n{verdict.answer}\n\nCONFIDENCE: {verdict.confidence:.0%}\nACCEPTED: {', '.join(verdict.accepted_agents)}\nUNCERTAIN: {'; '.join(verdict.unresolved_disputes) or 'none'}",
                round_num=round_num,
                vote_tags=["decision-verdict", f"conf={verdict.confidence:.0%}", f"rounds={verdict.rounds_taken}"],
                confidence=verdict.confidence,
            )
        except Exception as exc:
            self._log.error("decision_run_failed", error=str(exc))
            # Graceful degradation — return best available answer from board
            all_msgs = board.get_all()
            best = max(all_msgs, key=lambda m: m.confidence, default=None) if all_msgs else None
            fallback = best.content if best else "Could not determine a verdict."
            return self.make_message(
                content=fallback, round_num=round_num,
                vote_tags=["decision-fallback"], confidence=0.5,
            )

    # ── Parsing helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_agent_scores(data: dict) -> list[AgentScore]:
        scores = []
        for s in data.get("agent_scores", []):
            try:
                scores.append(AgentScore(
                    agent_id=s.get("agent_id", "unknown"),
                    role=s.get("role", "unknown"),
                    evidence_score=float(s.get("evidence_score", 50)),
                    stability_score=float(s.get("stability_score", 50)),
                    challenge_score=float(s.get("challenge_score", 0)),
                    overall=float(s.get("overall", 50)),
                    notes=s.get("notes", ""),
                ))
            except (TypeError, ValueError):
                pass
        return scores

    @staticmethod
    def _extract_section(text: str, section: str) -> str:
        m = re.search(
            rf"{section}:\s*(.*?)(?:ACCEPTED:|DISCARDED:|UNCERTAIN:|CONFIDENCE:|MODEL:|$)",
            text, re.S | re.I
        )
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_list(text: str, section: str) -> list[str]:
        m = re.search(
            rf"{section}:\s*(.*?)(?:UNCERTAIN:|CONFIDENCE:|MODEL:|VERDICT:|ACCEPTED:|DISCARDED:|$)",
            text, re.S | re.I
        )
        if not m:
            return []
        raw = m.group(1).strip()
        if not raw or raw.lower() in ("none", "n/a", "-"):
            return []
        return [line.strip().lstrip("-•* ") for line in raw.split("\n") if line.strip()]

    @staticmethod
    def _extract_confidence(text: str) -> Optional[float]:
        m = re.search(r"CONFIDENCE:\s*(\d+)%", text, re.I)
        return int(m.group(1)) / 100.0 if m else None

    @staticmethod
    def _ground_check(answer: str, verified_data: str) -> str:
        """
        Minimal grounding check: warn if the answer contains numbers
        that don't appear in verified_data. Appends a note if found.
        """
        import re as _re
        answer_nums = set(_re.findall(r"[\d,]+\.?\d*", answer))
        verified_nums = set(_re.findall(r"[\d,]+\.?\d*", verified_data))
        invented = answer_nums - verified_nums - {"0", "1", "2", "3"}
        if invented and len(invented) < 5:  # avoid false positives on dates etc
            answer += (
                f"\n\n[Grounding note: {len(invented)} value(s) in this answer "
                f"could not be traced to scraped sources. Verify independently.]"
            )
        return answer
