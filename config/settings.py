from __future__ import annotations
import os
"""Nexus AI — Central configuration. Zero paid APIs required."""
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
    )
    # Server — 127.0.0.1 locally, 0.0.0.0 in Docker/cloud (behind reverse proxy)
    host: str = Field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = Field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    debug: bool = Field(False)
    environment: Literal["development","staging","production"] = Field(
        default_factory=lambda: os.getenv("ENVIRONMENT", "development")
    )
    # Workers (use 1 for dev, match CPU count for production)
    workers: int = Field(default_factory=lambda: int(os.getenv("WORKERS", "1")))

    # Security
    jwt_keychain_service: str = "nexus-ai"
    jwt_keychain_username: str = "jwt_secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 7
    rate_limit_per_minute: int = 10
    max_request_body_kb: int = 64
    allowed_origins: list[str] = Field(default=["http://localhost:3000","http://127.0.0.1:3000","http://localhost:8000","http://127.0.0.1:8000"])

    # Database
    database_url: str = Field(f"sqlite+aiosqlite:///{ROOT}/data/nexus.db")
    audit_database_url: str = Field(f"sqlite+aiosqlite:///{ROOT}/data/audit.db")

    # ── LLM: Groq free tier PRIMARY (no Ollama required for basic use) ──────────
    # Groq free tier = 14,400 requests/day, fast inference, no credit card
    use_groq_primary: bool = Field(True, description="Use Groq free tier as primary LLM")
    groq_primary_model: str = "llama-3.3-70b-versatile"   # Groq free
    groq_fast_model: str = "llama3-8b-8192"               # Groq free, faster
    groq_reasoning_model: str = "deepseek-r1-distill-llama-70b"  # Groq free

    # Ollama optional (large models for privacy/offline use)
    use_ollama: bool = Field(False)
    ollama_base_url: str = Field("http://127.0.0.1:11434")
    orchestrator_model: str = "qwen2.5:72b"
    classifier_model: str = "llama3.2:3b"
    critic_model: str = "deepseek-r1:32b"
    researcher_model: str = "qwen2.5:72b"
    decision_model: str = "deepseek-r1:32b"
    embedding_model: str = "nomic-embed-text"
    orchestrator_fallbacks: list[str] = Field(default=["qwen2.5:32b","llama3.2:3b"])

    # ── API keys (all free) ──────────────────────────────────────────────────────
    groq_keychain_key: str = "groq_api_key"            # console.groq.com — FREE
    hf_keychain_key: str = "huggingface_token"         # huggingface.co — FREE
    telegram_keychain_key: str = "telegram_bot_token"   # @BotFather — FREE
    telegram_chat_id_keychain_key: str = "telegram_chat_id"
    spotify_client_id_key: str = "spotify_client_id"   # developer.spotify.com — FREE
    spotify_client_secret_key: str = "spotify_client_secret"
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    gmail_credentials_key: str = "gmail_credentials"   # Google Cloud Console — FREE
    serper_api_key: str = "serper_api_key"             # serper.dev — FREE 2500/month
    openweather_api_key: str = "openweather_api_key"   # openweathermap.org — FREE
    aviationstack_api_key: str = "aviationstack_api_key" # aviationstack.com — FREE 500/month
    alphavantage_api_key: str = "alphavantage_api_key" # alphavantage.co — FREE 500/day

    # ── Agent pipeline ─────────────────────────────────────────────────────────
    max_debate_rounds: int = 3
    convergence_threshold: float = 0.92
    max_browser_agents: int = 6
    browser_timeout_ms: int = 20000
    source_freshness_seconds: int = 300
    max_concurrent_tasks_per_user: int = 3
    task_max_retries: int = 3
    task_retry_backoff_s: float = 2.0

    # Latency tuning (keeps architecture unchanged, tightens execution budgets)
    fast_response_mode: bool = True
    simple_task_target_s: float = 4.0
    complex_task_target_s: float = 15.0
    llm_call_timeout_s: float = 8.0
    web_search_timeout_s: float = 4.0
    meeting_timeout_s: float = 5.0
    skip_meeting_for_simple_queries: bool = True

    # Cache TTLs (seconds)
    query_cache_enabled: bool = True
    cache_ttl_prices: int = 180       # 3 min — prices change fast
    cache_ttl_flights: int = 300      # 5 min
    cache_ttl_weather: int = 600      # 10 min
    cache_ttl_stocks: int = 60        # 1 min — markets move fast
    cache_ttl_knowledge: int = 3600   # 1 hour

    # Token budget (Groq free tier limits)
    daily_token_budget: int = 200_000  # conservative — well under Groq limit
    monthly_token_budget: int = 4_000_000

    # HITL
    hitl_expiry_hours: int = 24

    # Paths
    data_dir: Path = ROOT / "data"
    logs_dir: Path = ROOT / "data" / "logs"
    screenshots_dir: Path = ROOT / "data" / "screenshots"
    cache_dir: Path = ROOT / "data" / "cache"
    faiss_index_path: Path = ROOT / "data" / "cache" / "faiss.index"

    @field_validator("host")
    @classmethod
    def host_must_be_local(cls, v: str) -> str:
        if v == "0.0.0.0":
            raise ValueError("SECURITY: host must be 127.0.0.1")
        return v

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
