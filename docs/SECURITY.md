# Nexus AI — Security Model

## CVEs fixed by design

| CVE / Vulnerability | Fix |
|---|---|
| CVE-2026-25253 · WebSocket RCE (CVSS 8.8) | No WebSocket gateway. FastAPI on 127.0.0.1 only. JWT on every request. |
| Prompt injection via email/web | InputGuard + `<external>` XML wrapping on all external content. |
| Supply chain poisoning (820+ malicious skills) | No public skill registry. All tools hardcoded. No shell exec. |
| 40,000 exposed instances | Host binding validated in Settings. Refuses to start on 0.0.0.0. |
| Log poisoning via WebSocket | Agents have no DB handle to audit log. Write-only append stream. |
| Data exfiltration via link preview | Output URL filter + Presidio PII masker on all outbound content. |
| Lethal trifecta | No single agent holds: private data + external comms + untrusted content. |

## How to verify each fix

```bash
# Run all Phase 1 security tests
pytest tests/security/ -v

# Verify localhost binding
grep -n "host_must_be_local" config/settings.py

# Verify agent manifests are locked
grep -n "manifest" src/agents/  (Phase 2)

# Verify audit log is append-only
grep -n "UPDATE\|DELETE" src/security/audit_logger.py  # should return nothing
```
