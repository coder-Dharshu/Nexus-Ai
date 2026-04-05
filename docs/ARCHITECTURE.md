# Nexus AI — Architecture

See README.md for the full diagram.

## Agent roster (Phase 2+)

| Agent | Model | Tools | Role |
|---|---|---|---|
| Orchestrator | Qwen3-235B | task_assign, state_write | Master controller |
| Classifier | Llama 3.3 70B (Groq) | none | Routes queries in <1s |
| Researcher | Qwen2.5-72B | vector_search | Retrieves facts from memory |
| Reasoner | Qwen3-235B | none | Pure chain-of-thought |
| Drafter | Qwen2.5-72B | gmail_read | Tone-matched email drafting |
| Browser Agent ×6 | Qwen2.5-72B parser | browser | Live data scraping |
| Output Validator | Rule + DeepSeek-R1 | none | 5-layer check |
| Cross-verifier | Deterministic math | none | Weighted consensus |
| Critic | DeepSeek-R1-Distill-32B | none | Adversarial challenger |
| Fact-checker | DeepSeek-R1-Distill-32B | none | Historical baseline |
| Synthesizer | Qwen3-235B | hitl_trigger | Final answer formatter |
| Decision Agent | DeepSeek-R1-Distill-32B | none | Reads full transcript, verdicts |
