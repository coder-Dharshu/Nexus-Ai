# Phase 1 — Security foundation checklist

Complete every item here before starting Phase 2.
Run the full test suite and verify every box is ticked.

## Week 1 — Server hardening + auth

### Day 1 — Project setup
- [ ] `pyproject.toml` created with all Phase 1 deps
- [ ] `config/settings.py` with `host` validator (blocks `0.0.0.0`)
- [ ] `.env.example` created (no real secrets)
- [ ] `.gitignore` includes `.env`, `data/`, `secrets/`
- [ ] `data/` directories created (logs, screenshots, cache)

### Day 2 — JWT auth
- [ ] `src/security/keychain.py` — OS keychain wrapper
- [ ] `src/security/auth.py` — JWT create + decode + CSRF
- [ ] `scripts/setup.py` — first-run keychain initialization
- [ ] JWT secret generated and stored in keychain (not in .env)
- [ ] Test: `pytest tests/unit/test_auth.py -v` → all pass

### Day 3 — FastAPI server
- [ ] `src/api/main.py` — FastAPI with localhost binding middleware
- [ ] `src/api/routes/health.py` — `/health/ping` + `/health/ready`
- [ ] `src/api/routes/auth.py` — login, refresh, logout, me
- [ ] Server starts with `uvicorn src.api.main:app --host 127.0.0.1 --port 8000`
- [ ] Test: `curl http://127.0.0.1:8000/health/ping` → `{"status": "ok"}`

### Day 4 — Rate limiting
- [ ] `slowapi` rate limiter wired to FastAPI
- [ ] Rate limit: 10 req/min per authenticated user
- [ ] `429 Too Many Requests` returned on breach with `Retry-After` header
- [ ] Rate limit hits logged to audit table
- [ ] Test: 11 rapid requests → 10 succeed, 11th returns 429

### Day 5 — Database + audit log
- [ ] `src/utils/db.py` — SQLite schema, helper functions
- [ ] `src/security/audit_logger.py` — append-only audit log
- [ ] Tables created: `users`, `tasks`, `approval_queue`, `audit_log`
- [ ] Verified: no UPDATE/DELETE on `audit_log` table anywhere in codebase
- [ ] Verified: no agent module imports `audit_logger`

---

## Week 2 — Input guard + agent sandbox

### Day 1 (Mon) — Input guard
- [ ] `src/security/input_guard.py` — pattern + scoring classifier
- [ ] 20+ injection patterns defined and tested
- [ ] External content always wrapped in `<external>` tags
- [ ] Test: `pytest tests/unit/test_input_guard.py -v` → all pass
- [ ] Test: `pytest tests/security/test_phase1_security.py::TestInputGuard -v` → all pass

### Day 2 (Tue) — Agent manifest structure
- [ ] `AgentManifest` data class defined (tool list locked at init)
- [ ] Lethal trifecta check: no agent holds all 3 dangerous capabilities
- [ ] Test: assigning send_email to researcher raises `PermissionError`
- [ ] Test: assigning web_search to drafter raises `PermissionError`

### Day 3 (Wed) — PII masker
- [ ] `src/security/pii_masker.py` — Presidio + custom regex
- [ ] Covers: email, phone, Aadhaar, PAN, API keys, Bearer tokens, AWS keys
- [ ] Masker applied to all outbound messages (route output, Telegram)
- [ ] Test: `pytest tests/unit/test_pii_masker.py -v` → all pass

### Day 4 (Thu) — Output URL filter + domain whitelist
- [ ] URL filter: strips URLs with query params > 20 chars from output
- [ ] Domain whitelist: agents can only call whitelisted domains
- [ ] Test: agent tries unlisted domain → blocked + audit log entry
- [ ] Test: URL with encoded data in query string → stripped

### Day 5 (Fri) — Full security audit
- [ ] `pytest tests/security/ -v` → ALL tests pass
- [ ] `pytest tests/unit/ -v` → ALL tests pass
- [ ] `pytest tests/integration/ -v` → ALL tests pass
- [ ] Grep for `0.0.0.0` in entire codebase → zero results
- [ ] Grep for `os.environ` → only in `settings.py` (pydantic allowed)
- [ ] Grep for hardcoded secrets → zero results
- [ ] `SECURITY.md` written documenting all protections
- [ ] Git tag: `v0.1.0-security-foundation`

---

## Verification commands

```bash
# Run all Phase 1 tests
pytest tests/security/ tests/unit/ -v --tb=short

# Verify no 0.0.0.0 bindings
grep -r "0.0.0.0" src/ config/ --include="*.py"
# Expected output: (none)

# Verify no plaintext secrets
grep -r "sk-" src/ config/ --include="*.py"
grep -r "hf_" src/ config/ --include="*.py"
# Expected output: (none, only keychain key names allowed)

# Verify audit log is append-only
grep -r "audit_log" src/ --include="*.py" | grep -v "INSERT\|SELECT\|record\|get_recent\|count_by"
# Expected output: (none — no UPDATE/DELETE on audit_log)

# Start server and verify binding
uvicorn src.api.main:app --host 127.0.0.1 --port 8000 &
curl http://127.0.0.1:8000/health/ping
# Expected: {"status":"ok","version":"0.1.0","binding":"127.0.0.1 (secure)"}

# Verify rate limiting
for i in $(seq 1 12); do curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/health/ping; done
# Expected: 10 × 200, then 429
```

---

## Phase 1 → Phase 2 gate

**DO NOT start Phase 2 until:**
1. `pytest tests/security/ -v` → 0 failures
2. `pytest tests/unit/ -v` → 0 failures  
3. Server starts and `/health/ready` returns `{"status": "ready"}`
4. All items above are checked

Phase 2 builds the core agent pipeline. If the security foundation has gaps,
the agents will inherit those gaps. Fix security first.
