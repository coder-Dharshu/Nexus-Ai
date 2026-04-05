"""Nexus AI — Agents package (all improvements included)."""
from src.agents.base import BaseAgent, MessageBoard, AgentMessage, LockedManifest
from src.agents.classifier import QueryClassifier, QueryType, ClassificationResult
from src.agents.orchestrator import OrchestratorAgent, OrchestratorPlan
from src.agents.researcher import ResearcherAgent
from src.agents.reasoner import ReasonerAgent
from src.agents.critic import CriticAgent
from src.agents.fact_checker import FactCheckerAgent
from src.agents.synthesizer import SynthesizerAgent
from src.agents.verifier import VerifierAgent
from src.agents.adaptive_debate import get_debate_config, DebateConfig
from src.agents.domain_agents import (
    FinanceAgent, TravelAgent, LegalAgent, MedicalAgent, get_domain_agent
)
from src.agents.llm_client import LLMClient, MockLLMClient, llm_client

__all__ = [
    "BaseAgent", "MessageBoard", "AgentMessage", "LockedManifest",
    "QueryClassifier", "QueryType", "ClassificationResult",
    "OrchestratorAgent", "OrchestratorPlan",
    "ResearcherAgent", "ReasonerAgent", "CriticAgent",
    "FactCheckerAgent", "SynthesizerAgent", "VerifierAgent",
    "get_debate_config", "DebateConfig",
    "FinanceAgent", "TravelAgent", "LegalAgent", "MedicalAgent", "get_domain_agent",
    "LLMClient", "MockLLMClient", "llm_client",
]
