<div align="center">
  <h1>Nexus AI v2.0</h1>
  <p><strong>Multi-agent AI that argues before it answers.</strong></p>
  <p>
    <img src="https://img.shields.io/badge/version-2.0.0-blue?style=flat-square"/>
    <img src="https://img.shields.io/badge/python-3.11+-green?style=flat-square"/>
    <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square"/>
    <img src="https://img.shields.io/badge/cost-$0/month-brightgreen?style=flat-square"/>
    <img src="https://img.shields.io/badge/CVEs-0_open-brightgreen?style=flat-square"/>
  </p>
</div>

---

## Why Nexus over OpenClaw?

| | OpenClaw | Nexus AI |
|---|---|---|
| Price accuracy | Training data only | 6 live sources, cross-verified |
| Agent debate | Single LLM call | 4 agents, 3 rounds, convergence check |
| CVEs | 7 documented | 0 open (4 found + patched) |
| HITL gate | Optional | Mandatory for every irreversible action |
| Hallucination guard | None | Grounding gate + post-decision verifier |
| Source transparency | No | Full transcript + citations API |
| Host binding | 0.0.0.0 (CVE) | Validated at startup |
| Audit log | Mutable | SHA-256 hash-chained, tamper-evident |
| Trifecta check | No | Enforced — no agent holds all 3 risky capabilities |

---

## Quick start (90 seconds)

```bash
# 1. Install
curl -fsSL https://raw.githubusercontent.com/your-org/nexus-ai/main/install.sh | bash

# 2. Configure (add Groq key — free at console.groq.com)
nexus setup

# 3. Start
nexus start
# → http://127.0.0.1:8000
```

**Windows:** double-click `install-windows.bat`

---

## Deploy to cloud (one command each)

### Railway
```bash
railway login && railway up
# Set env: GROQ_API_KEY, JWT_SECRET
```

### Render
```bash
# Connect GitHub repo in Render dashboard
# Select render.yaml — auto-detected
# Add env vars: GROQ_API_KEY, JWT_SECRET
```

### Fly.io
```bash
flyctl launch --config fly.toml
flyctl secrets set GROQ_API_KEY=your_key JWT_SECRET=$(openssl rand -hex 32)
flyctl deploy
```

### Docker (self-hosted)
```bash
docker compose -f docker/docker-compose.prod.yml up -d
```

### Kubernetes
```bash
kubectl apply -f k8s/
kubectl create secret generic nexus-secrets \
  --from-literal=groq-api-key=$GROQ_API_KEY \
  --from-literal=jwt-secret=$(openssl rand -hex 32)
```

---

## API reference

```
POST /auth/login              JWT token pair
POST /auth/refresh            Refresh token
POST /auth/logout             Revoke token

POST /tasks/query             Submit query → task_id + stream_url
GET  /tasks/{id}              Poll status + result
GET  /tasks/{id}/stream       SSE live pipeline events
WS   /stream/ws/{id}?token=X  WebSocket live pipeline events
GET  /tasks/dead-letter        Failed tasks (max retries exhausted)

GET  /insights/{id}/transcript  Full agent debate transcript
GET  /insights/{id}/sources     All scraped sources + trust scores
GET  /insights/{id}/confidence  Confidence breakdown
GET  /insights/token-usage      Daily token consumption
GET  /insights/trust-scores     Per-source adaptive trust scores

POST /workspace/              Create workspace
POST /workspace/{id}/invite   Invite member
GET  /workspace/{id}/tasks    Shared task history

GET  /health/ping             Liveness
GET  /health/ready            Readiness (DB + LLM)
```

---

## Architecture

```
User query
  → Input Guard v2 (injection · homoglyph · invisible unicode — CVE-NX-001/002 patched)
  → Classifier (Groq llama3-8b, <1s, location-aware)
  → Query Cache (SHA-256 keyed, TTL per subtype)

Live data path:
  → 6× Playwright browser agents (parallel)
  → 5-layer validator (freshness · format · outlier · DOM · trust)
  → Cross-verifier (weighted consensus, spread check)
  → Grounding gate (LLM never sees raw data — only verified JSON)

Knowledge path:
  → FAISS vector memory search
  → Agent meeting room: Researcher → Reasoner → Critic → Fact-checker
  → Debate convergence check (cosine similarity ≥ 0.92)

Action path:
  → Drafter (tone-matched from history · cold email for new contacts)
  → HITL gate (Telegram approval, 24h expiry)
  → Task executor (Spotify · Gmail · Google Calendar)

All paths:
  → Decision Agent (reads full transcript, scores all agents)
  → Post-decision Verifier (cross-checks every number vs source data)
  → Output Sanitizer (homoglyph · base64 · exfil URL · injection)
  → PII Masker (Presidio + Aadhaar/PAN patterns)
  → Answer with citations + confidence score
```

---

## Feature set

**Live data (real-time, no training data)**
- Gold/silver/oil/petrol price — location-aware (India: IBJA/MCX/INR, UK: Kitco/GBP, US: APMEX/USD)
- Flights — 6 platforms compared (Google, MakeMyTrip, Skyscanner, Ixigo, Paytm, Cleartrip)
- Stocks — NSE NIFTY 50/SENSEX via official API, Yahoo Finance for global indices
- Crypto — CoinGecko free API (no key, no rate limit for basic use)
- Weather — open-meteo.com (completely free, no key)
- Currency — open.er-api.com (free tier, no key)

**Actions (HITL-gated)**
- Email — Gmail API + SMTP fallback, tone-matched drafts, cold email for first contact
- Music — Spotify play/pause/skip/volume/search via Web API
- Calendar — Google Calendar create/list events
- Reminders — APScheduler local reminders
- Web search — Serper.dev (2500/month free) + DuckDuckGo fallback

**Security (7-layer)**
- Layer 1: Input guard v2 — 30+ injection patterns, Cyrillic normalization, invisible unicode
- Layer 2: JWT + bcrypt + JTI revocation blacklist + timing-safe login
- Layer 3: Output sanitizer — base64 decode check, exfil URL, homoglyph, injection
- Layer 4: PII masker — Aadhaar, PAN, phone, email, credit card
- Layer 5: Per-user rate limiter — bot detection, account lock
- Layer 6: SHA-256 hash-chained audit log — tamper-evident, verified nightly
- Layer 7: Nginx TLS 1.3, HSTS preload, OCSP stapling, 0.0.0.0 binding rejected

**Transparency (not present in OpenClaw)**
- Full debate transcript API per task
- Per-source trust scores (adaptive EMA, updated every query)
- Confidence breakdown (source vs agent vs verifier)
- Token usage tracking per user per day

---

## Required API keys (all free)

| Key | Required | Source | Cost |
|---|---|---|---|
| Groq API key | Yes for AI | console.groq.com | Free, 14.4k req/day |
| Telegram bot token | Optional (notifications) | @BotFather | Free |
| Spotify credentials | Optional (music) | developer.spotify.com | Free |
| Gmail OAuth | Optional (email) | console.cloud.google.com | Free |
| Serper API key | Optional (web search) | serper.dev | Free 2500/month |
| OpenAI / Anthropic | Never needed | — | Not needed |

---

## Cost: $0/month

All AI inference via Groq free tier (14,400 req/day). All data from free APIs (CoinGecko, open-meteo, NSE India). All web scraping via local Playwright. Database is SQLite. No cloud AI subscription needed.

---

## License

MIT License — use freely, modify, deploy commercially.
