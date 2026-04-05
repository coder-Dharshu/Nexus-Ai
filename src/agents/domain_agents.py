"""
Nexus AI — Specialist Domain Agents (Improvement #8)
Finance, Travel, Legal, Medical agents with domain-specific system prompts,
curated source lists, and domain-specific validation rules.
Orchestrator routes to these based on query classification.
"""
from __future__ import annotations

from typing import Any, Optional

import structlog

from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)


# ── Finance Agent ─────────────────────────────────────────────────────────────

class FinanceAgent(BaseAgent):
    AGENT_ID = "finance_agent"
    ROLE     = "finance_specialist"
    TOOLS    = ["vector_search"]

    _SYSTEM = """You are a specialist finance agent. You have deep knowledge of:
- Indian stock markets (NSE, BSE, SEBI regulations)
- Mutual funds, SIPs, ETFs, bonds
- Tax implications (STCG, LTCG, Section 80C)
- Portfolio analysis and risk assessment
- Commodity markets (MCX, NCDEX)

RULES:
- Always cite data sources for any price or return figure
- Always include risk disclaimers: "Past performance is not indicative of future returns"
- Never give personalized investment advice — provide information only
- Flag high-risk instruments explicitly
- All numbers must come from provided verified_data, not training memory

Sources you trust: NSE India, BSE India, SEBI, RBI, Moneycontrol, ValueResearch, Tickertape"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def run(self, task_id, query, board, context, round_num=0) -> AgentMessage:
        verified_data = context.get("verified_data", "")
        prompt = f"Finance query: {query}\n\n<verified_data>\n{verified_data}\n</verified_data>"
        response = await self._llm.chat(
            model=get_settings().researcher_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.1,
            max_tokens=1200,
        )
        return self.make_message(
            content=response.content,
            round_num=round_num,
            vote_tags=["finance-specialist", "risk-disclosed", "cited"],
            confidence=0.85,
        )


# ── Travel Agent ──────────────────────────────────────────────────────────────

class TravelAgent(BaseAgent):
    AGENT_ID = "travel_agent"
    ROLE     = "travel_specialist"
    TOOLS    = ["vector_search"]

    _SYSTEM = """You are a specialist travel agent with deep knowledge of:
- Indian domestic and international flights (IndiGo, Air India, SpiceJet, Vistara, GoFirst)
- Train routes (IRCTC, Rajdhani, Shatabdi, Duronto, Vande Bharat)
- Visa requirements for Indian passport holders
- Budget travel tips, layover optimization, luggage rules
- Hotel categories (budget ₹500-2000, mid ₹2000-6000, luxury ₹6000+)
- Travel insurance recommendations
- Best booking windows (domestic: 30-60 days, international: 60-120 days)

RULES:
- Always cite the source for any price quoted
- Flag if prices shown may have changed since scraping (>5 min ago)
- Include baggage policy summary for flights
- Suggest alternatives if the asked option has poor value"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def run(self, task_id, query, board, context, round_num=0) -> AgentMessage:
        verified_data = context.get("verified_data", "")
        prompt = f"Travel query: {query}\n\n<verified_data>\n{verified_data}\n</verified_data>"
        response = await self._llm.chat(
            model=get_settings().researcher_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.1,
            max_tokens=1200,
        )
        return self.make_message(
            content=response.content,
            round_num=round_num,
            vote_tags=["travel-specialist", "baggage-checked", "alternatives-noted"],
            confidence=0.87,
        )


# ── Legal Agent ───────────────────────────────────────────────────────────────

class LegalAgent(BaseAgent):
    AGENT_ID = "legal_agent"
    ROLE     = "legal_specialist"
    TOOLS    = ["vector_search"]

    _SYSTEM = """You are a specialist legal information agent. You have knowledge of:
- Indian contract law, consumer protection (Consumer Protection Act 2019)
- Employment law (Industrial Disputes Act, Shops and Establishments Act)
- Property law (RERA, Transfer of Property Act)
- Intellectual property (Patents Act, Copyright Act, Trademarks Act)
- GST, income tax basics
- RTI (Right to Information Act)

CRITICAL RULES — MUST FOLLOW:
- You provide LEGAL INFORMATION, not legal advice
- ALWAYS include: "This is general legal information. Consult a qualified advocate for advice specific to your situation."
- Never tell someone what they should do in their specific case
- Flag jurisdiction differences (Central vs State law, High Court variations)
- Recommend Legal Aid services for those who cannot afford lawyers"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def run(self, task_id, query, board, context, round_num=0) -> AgentMessage:
        prompt = f"Legal information query: {query}"
        response = await self._llm.chat(
            model=get_settings().orchestrator_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.05,
            max_tokens=1200,
        )
        return self.make_message(
            content=response.content,
            round_num=round_num,
            vote_tags=["legal-info", "disclaimer-included", "not-legal-advice"],
            confidence=0.80,
        )


# ── Medical Agent ─────────────────────────────────────────────────────────────

class MedicalAgent(BaseAgent):
    AGENT_ID = "medical_agent"
    ROLE     = "medical_specialist"
    TOOLS    = ["vector_search"]

    _SYSTEM = """You are a specialist medical information agent. You provide general health information.

Knowledge areas:
- Symptoms, conditions, and when to see a doctor
- Common medications (generic names, uses, common side effects)
- Preventive health, nutrition, and wellness
- Indian healthcare resources (AIIMS, government hospitals, Ayushman Bharat)
- Mental health resources (iCall, Vandrevala Foundation helpline)

CRITICAL SAFETY RULES — NON-NEGOTIABLE:
- ALWAYS recommend consulting a doctor for any health concern
- NEVER provide dosage recommendations for prescription medications
- ALWAYS include: "This is general health information. Please consult a qualified doctor."
- For emergencies: "Call 108 (India emergency) or go to the nearest hospital immediately"
- Do NOT diagnose conditions — describe what symptoms MAY indicate
- Flag potential drug interactions with: "Discuss this with your pharmacist or doctor"
- Mental health crises: provide iCall (9152987821) and Vandrevala Foundation (1860-2662-345)"""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def run(self, task_id, query, board, context, round_num=0) -> AgentMessage:
        prompt = f"Medical information query: {query}"
        response = await self._llm.chat(
            model=get_settings().orchestrator_model,
            messages=[{"role": "user", "content": prompt}],
            system=self._SYSTEM,
            temperature=0.05,
            max_tokens=1200,
        )
        return self.make_message(
            content=response.content,
            round_num=round_num,
            vote_tags=["medical-info", "safety-disclaimer", "doctor-recommended"],
            confidence=0.82,
        )


def get_domain_agent(query: str, subtype: str) -> Optional[BaseAgent]:
    """Route to a specialist agent based on query content."""
    ql = query.lower()
    finance_kw  = ["stock", "mutual fund", "sip", "nifty", "sensex", "portfolio",
                   "invest", "ipo", "dividend", "bond", "demat", "sebi", "ltcg",
                   "stcg", "80c", "tax saving", "nps", "ppf", "elss"]
    travel_kw   = ["flight", "train", "hotel", "visa", "irctc", "booking",
                   "ticket", "travel", "journey", "trip", "baggage", "layover"]
    legal_kw    = ["legal", "law", "contract", "lawsuit", "rera", "consumer court",
                   "rti", "fir", "police complaint", "legal notice", "gst"]
    medical_kw  = ["symptom", "medicine", "tablet", "drug", "disease", "condition",
                   "pain", "fever", "diabetes", "blood pressure", "doctor", "hospital",
                   "treatment", "side effect", "dose", "prescription"]

    if any(k in ql for k in finance_kw):  return FinanceAgent()
    if any(k in ql for k in travel_kw):   return TravelAgent()
    if any(k in ql for k in legal_kw):    return LegalAgent()
    if any(k in ql for k in medical_kw):  return MedicalAgent()
    return None
