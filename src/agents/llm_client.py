"""
Nexus AI — LLM Client
PRIMARY: Groq free tier (no install, just API key from console.groq.com)
FALLBACK: Ollama local → HuggingFace free
"""
from __future__ import annotations
import asyncio, hashlib, json, time
from dataclasses import dataclass, field
from typing import Optional
import structlog
from config.settings import get_settings
log = structlog.get_logger(__name__)
_s = get_settings()

@dataclass
class LLMResponse:
    content: str; model: str; prompt_tokens: int = 0
    completion_tokens: int = 0; latency_ms: float = 0.0
    backend: str = ""; cached: bool = False
    @property
    def total_tokens(self): return self.prompt_tokens + self.completion_tokens

@dataclass
class CircuitBreaker:
    name: str; failure_threshold: int = 3; recovery_seconds: float = 60.0
    _failures: int = field(default=0, repr=False)
    _opened_at: float = field(default=0.0, repr=False)
    @property
    def is_open(self):
        if self._failures >= self.failure_threshold:
            if time.time() - self._opened_at < self.recovery_seconds: return True
            self._failures = 0
        return False
    def record_failure(self):
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.time()
            log.warning("circuit_opened", backend=self.name)
    def record_success(self):
        if self._failures > 0: log.info("circuit_closed", backend=self.name)
        self._failures = 0

class ResponseCache:
    def __init__(self): self._store: dict = {}
    def _key(self, m, msgs):
        return hashlib.sha256(json.dumps({"m":m,"msgs":msgs},sort_keys=True).encode()).hexdigest()[:16]
    def get(self, m, msgs):
        k = self._key(m, msgs); e = self._store.get(k)
        if not e: return None
        r, exp = e
        if time.time() > exp: del self._store[k]; return None
        return r
    def set(self, m, msgs, r, ttl=300):
        k = self._key(m, msgs)
        cr = LLMResponse(content=r.content, model=r.model, backend=r.backend, cached=True,
                         prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens)
        self._store[k] = (cr, time.time() + ttl)

class TokenTracker:
    def __init__(self): self._daily: dict = {}; self._total = 0
    def record(self, uid, tokens):
        self._daily[uid] = self._daily.get(uid, 0) + tokens; self._total += tokens
        if self._daily[uid] > _s.daily_token_budget * 0.8:
            log.warning("token_budget_warn", uid=uid, used=self._daily[uid])
    def usage(self, uid): return {"today": self._daily.get(uid,0), "total": self._total, "budget": _s.daily_token_budget}

class LLMClient:
    GROQ   = "https://api.groq.com/openai/v1/chat/completions"
    OLLAMA = "http://127.0.0.1:11434/api/chat"
    HF     = "https://api-inference.huggingface.co/models/{model}/v1/chat/completions"

    def __init__(self, timeout=60.0):
        self._timeout = timeout; self._cache = ResponseCache(); self._tracker = TokenTracker()
        self._cb = {"groq": CircuitBreaker("groq"), "ollama": CircuitBreaker("ollama"), "hf": CircuitBreaker("hf")}
        self._ollama_ok: Optional[bool] = None

    async def chat(self, model: str, messages: list, *, system: Optional[str]=None,
                   temperature=0.3, max_tokens=2048, json_mode=False,
                   user_id="system", cache_ttl=0) -> LLMResponse:
        all_msgs = ([{"role":"system","content":system}] if system else []) + messages
        if cache_ttl > 0:
            hit = self._cache.get(model, all_msgs)
            if hit: return hit
        r = await self._try_all(model, all_msgs, temperature, max_tokens, json_mode)
        self._tracker.record(user_id, r.total_tokens)
        if cache_ttl > 0: self._cache.set(model, all_msgs, r, ttl=cache_ttl)
        return r

    async def _try_all(self, model, msgs, temp, max_tok, json_mode):
        import httpx
        errors = []

        # 1. Groq free tier (primary — fastest, no install)
        if not self._cb["groq"].is_open:
            try:
                from src.security.keychain import secrets_manager
                key = secrets_manager.get(_s.groq_keychain_key, required=False)
                if key:
                    groq_model = self._to_groq(model)
                    t0 = time.perf_counter()
                    payload = {"model":groq_model,"messages":msgs,"temperature":temp,"max_tokens":max_tok}
                    if json_mode: payload["response_format"] = {"type":"json_object"}
                    async with httpx.AsyncClient(timeout=self._timeout) as c:
                        r = await c.post(self.GROQ, json=payload,
                                         headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"})
                        r.raise_for_status(); d = r.json()
                    lat = round((time.perf_counter()-t0)*1000,1)
                    u = d.get("usage",{})
                    self._cb["groq"].record_success()
                    log.info("groq_ok", model=groq_model, ms=lat)
                    return LLMResponse(content=d["choices"][0]["message"]["content"], model=groq_model,
                                       prompt_tokens=u.get("prompt_tokens",0),
                                       completion_tokens=u.get("completion_tokens",0),
                                       latency_ms=lat, backend="groq")
            except Exception as e:
                self._cb["groq"].record_failure(); errors.append(f"groq:{e}")

        # 2. Ollama local (optional, for privacy)
        if _s.use_ollama and not self._cb["ollama"].is_open:
            if self._ollama_ok is None:
                try:
                    async with httpx.AsyncClient(timeout=3) as c:
                        r = await c.get("http://127.0.0.1:11434/api/tags")
                        self._ollama_ok = r.status_code == 200
                except: self._ollama_ok = False
            if self._ollama_ok:
                try:
                    t0 = time.perf_counter()
                    payload = {"model":model,"messages":msgs,"stream":False,"options":{"temperature":temp,"num_predict":max_tok}}
                    if json_mode: payload["format"] = "json"
                    async with httpx.AsyncClient(timeout=self._timeout) as c:
                        r = await c.post(self.OLLAMA, json=payload); r.raise_for_status(); d = r.json()
                    lat = round((time.perf_counter()-t0)*1000,1)
                    self._cb["ollama"].record_success()
                    return LLMResponse(content=d.get("message",{}).get("content",""),model=model,
                                       prompt_tokens=d.get("prompt_eval_count",0),
                                       completion_tokens=d.get("eval_count",0),
                                       latency_ms=lat, backend="ollama")
                except Exception as e:
                    self._cb["ollama"].record_failure(); errors.append(f"ollama:{e}")

        # 3. HuggingFace free
        if not self._cb["hf"].is_open:
            try:
                from src.security.keychain import secrets_manager
                token = secrets_manager.get(_s.hf_keychain_key, required=False)
                if token:
                    t0 = time.perf_counter()
                    url = self.HF.format(model="Qwen/Qwen2.5-72B-Instruct")
                    async with httpx.AsyncClient(timeout=self._timeout) as c:
                        r = await c.post(url, json={"model":model,"messages":msgs,"temperature":temp,"max_tokens":max_tok},
                                         headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"})
                        r.raise_for_status(); d = r.json()
                    lat = round((time.perf_counter()-t0)*1000,1)
                    self._cb["hf"].record_success()
                    u = d.get("usage",{})
                    return LLMResponse(content=d["choices"][0]["message"]["content"], model=model,
                                       prompt_tokens=u.get("prompt_tokens",0),
                                       completion_tokens=u.get("completion_tokens",0),
                                       latency_ms=lat, backend="huggingface")
            except Exception as e:
                self._cb["hf"].record_failure(); errors.append(f"hf:{e}")

        log.error("all_llm_backends_failed", errors=str(errors)[:200])
        return LLMResponse(content="[LLM unavailable. Set Groq API key: nexus setup]",
                           model=model, backend="degraded")

    @staticmethod
    def _to_groq(model):
        if "deepseek" in model or "r1" in model: return "deepseek-r1-distill-llama-70b"
        if "72b" in model or "qwen" in model or "orchestrator" in model: return "qwen-qwq-32b"
        if "3b" in model or "classifier" in model: return "llama3-8b-8192"
        return "llama-3.3-70b-versatile"

    async def health_check(self):
        results = {}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get("http://127.0.0.1:11434/api/tags")
                results["ollama"] = r.status_code == 200
        except: results["ollama"] = False
        # Check if Groq key is set
        try:
            from src.security.keychain import secrets_manager
            key = secrets_manager.get(_s.groq_keychain_key, required=False)
            results["groq"] = bool(key)
        except: results["groq"] = False
        return results

class MockLLMClient(LLMClient):
    def __init__(self, responses=None):
        self._mock_responses = responses or {}; self._call_log = []
        self._cache = ResponseCache(); self._tracker = TokenTracker()
        self._cb = {}; self._ollama_ok = False
    async def chat(self, model, messages, **kwargs):
        last = next((m["content"] for m in reversed(messages) if m["role"]=="user"), "")
        content = self._mock_responses.get(model, self._mock_responses.get("default","Mock."))
        self._call_log.append({"model":model,"input":last,"output":content})
        return LLMResponse(content=content, model=model, backend="mock",
                           prompt_tokens=len(last.split()), completion_tokens=len(content.split()))
    async def health_check(self): return {"groq":True,"ollama":False}
    @property
    def calls(self): return list(self._call_log)

llm_client = LLMClient()
