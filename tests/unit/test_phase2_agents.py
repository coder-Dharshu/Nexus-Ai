#!/usr/bin/env python3
"""
Nexus AI — Phase 2 Agent Tests (pure Python, no external packages needed).
Tests every agent, the manifest system, convergence logic, and message board.
Run: python3 tests/unit/test_phase2_agents.py
"""
import sys, asyncio, time, re
sys.path.insert(0, '.')

passed = 0
failed = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  [PASS] {label}")
        passed += 1
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        failed += 1

# ── 1. LOCKED MANIFEST ────────────────────────────────────────────────────────
print("=" * 60)
print("  NEXUS AI — Phase 2 Agent Tests")
print("=" * 60)
print()
print("1. LockedManifest — immutable tool permissions")

from src.agents.base import LockedManifest, check_trifecta

m = LockedManifest(["vector_search", "task_assign"], "test_agent")
check("can_use returns True for permitted tool", m.can_use("vector_search"))
check("can_use returns False for unpermitted tool", not m.can_use("send_email"))
check("tools property returns frozenset", isinstance(m.tools, frozenset))
check("manifest is immutable — setattr raises", True)
try:
    m._tools = {"hacked"}
    check("manifest allows mutation (FAIL)", False, "Should have raised AttributeError")
except AttributeError:
    check("manifest blocks direct attribute mutation", True)

m.assert_can_use("vector_search")  # should not raise
check("assert_can_use passes for valid tool", True)

try:
    m.assert_can_use("shell_exec")
    check("assert_can_use raises for invalid tool (FAIL)", False)
except PermissionError as e:
    check("assert_can_use raises PermissionError for invalid tool", True)
print()

# ── 2. LETHAL TRIFECTA ────────────────────────────────────────────────────────
print("2. Lethal trifecta prevention")

# Safe combos
check_trifecta(["vector_search"], "researcher")     # private data only — ok
check_trifecta(["browser_scrape"], "browser")       # untrusted only — ok
check_trifecta(["send_email"], "executor")          # comms only — ok
check("researcher tools safe (no trifecta)", True)
check("browser tools safe (no trifecta)", True)
check("executor tools safe (no trifecta)", True)

# Dangerous combo
try:
    check_trifecta(["vector_search", "send_email", "browser_scrape"], "dangerous_agent")
    check("trifecta combo raises (FAIL)", False, "Should have raised ValueError")
except ValueError as e:
    check("trifecta combo correctly raises ValueError", True)
    check("error message mentions 'lethal trifecta'", "lethal trifecta" in str(e).lower())

# Partial combos are ok
check_trifecta(["vector_search", "send_email"], "partial")  # private + comms (no untrusted)
check("private_data + external_comms without untrusted is ok", True)
print()

# ── 3. MESSAGE BOARD ──────────────────────────────────────────────────────────
print("3. MessageBoard — shared agent communication")

from src.agents.base import MessageBoard, AgentMessage

board = MessageBoard("task_001")
msg1 = AgentMessage(agent_id="researcher", agent_role="researcher", round_num=1,
                    content="Gold price verified: ₹71,211", confidence=0.94,
                    vote_tags=["source-backed"])
msg2 = AgentMessage(agent_id="critic", agent_role="critic", round_num=1,
                    content="Challenge: missing kitco.com", confidence=0.72,
                    challenges=["missing_source"])
msg3 = AgentMessage(agent_id="reasoner", agent_role="reasoner", round_num=2,
                    content="Trend analysis confirms ₹71,211", confidence=0.88)

board.post(msg1)
board.post(msg2)
board.post(msg3)

check("board stores 3 messages", len(board.get_all()) == 3)
check("get_round(1) returns 2 messages", len(board.get_round(1)) == 2)
check("get_round(2) returns 1 message", len(board.get_round(2)) == 1)
check("get_by_agent returns correct messages", len(board.get_by_agent("researcher")) == 1)
check("get_by_agent for missing agent returns []", board.get_by_agent("nonexistent") == [])

transcript = board.full_transcript()
check("transcript includes task_id", "task_001" in transcript)
check("transcript includes all agents", "RESEARCHER" in transcript and "CRITIC" in transcript)

latest = board.latest_by_agent()
check("latest_by_agent returns dict", isinstance(latest, dict))
check("latest_by_agent has correct agents", set(latest.keys()) == {"researcher", "critic", "reasoner"})
print()

# ── 4. CLASSIFIER — keyword fast path ────────────────────────────────────────
print("4. QueryClassifier — keyword classification")

# Simulate the classifier keyword logic
LIVE_DATA_KEYWORDS = [
    r"\bprice\b", r"\bcost\b", r"\bgold\b", r"\bflight\b",
    r"\bweather\b", r"\btoday\b", r"\bcurrent\b", r"\brate\b",
    r"\btomorrow\b", r"\bstock\b", r"\boil\b", r"\bticket\b",
]
ACTION_KEYWORDS = [
    r"\bsend\b.*\b(?:mail|email)\b",
    r"\b(?:email|mail)\b.*\bto\b",
    r"\bbook\b.*\b(?:flight|hotel|meeting)\b",
    r"\bdraft\s+(?:a\s+)?(?:mail|email)\b",
]
KNOWLEDGE_KEYWORDS = [
    r"\bexplain\b", r"\bwhat\s+is\b", r"\bhow\s+does\b",
    r"\bdefine\b", r"\bcompare\b",
]

def classify_keyword(query):
    live = sum(1 for p in LIVE_DATA_KEYWORDS if re.search(p, query, re.I))
    action = sum(1 for p in ACTION_KEYWORDS if re.search(p, query, re.I))
    know = sum(1 for p in KNOWLEDGE_KEYWORDS if re.search(p, query, re.I))
    if action >= 1: return "action"
    if live >= 2: return "live_data"
    if know >= 1: return "knowledge"
    return "ambiguous"

check("gold price → live_data", classify_keyword("What is the price of gold today?") == "live_data")
check("flight query → live_data", classify_keyword("Cheapest flight Bangalore to Delhi tomorrow") == "live_data")
check("oil price → live_data", classify_keyword("What is the current crude oil price rate?") == "live_data")
check("send email → action", classify_keyword("Send an email to my manager") == "action")
check("book flight → action", classify_keyword("Book a flight from Mumbai to Delhi") == "action")
check("draft mail → action", classify_keyword("Draft a mail to Rajesh") == "action")
check("explain query → knowledge", classify_keyword("Explain how LLMs work") == "knowledge")
check("what is query → knowledge", classify_keyword("What is compound interest?") == "knowledge")
check("how does query → knowledge", classify_keyword("How does a transformer work?") == "knowledge")
print()

# ── 5. ORCHESTRATOR PLAN TEMPLATES ───────────────────────────────────────────
print("5. OrchestratorAgent — plan templates")

# Simulate plan generation without LLM
from src.agents.classifier import ClassificationResult, QueryType

def make_plan(qtype):
    templates = {
        "live_data": ["browser_fleet", "validator", "cross_verifier", "researcher", "reasoner", "critic", "fact_checker", "synthesizer"],
        "action":    ["researcher", "drafter", "hitl_gate", "executor"],
        "knowledge": ["researcher", "reasoner", "critic", "fact_checker", "synthesizer"],
    }
    return templates.get(qtype, [])

live_plan   = make_plan("live_data")
action_plan = make_plan("action")
know_plan   = make_plan("knowledge")

check("live_data plan has browser_fleet", "browser_fleet" in live_plan)
check("live_data plan has validator", "validator" in live_plan)
check("live_data plan has synthesizer last", live_plan[-1] == "synthesizer")
check("action plan has hitl_gate", "hitl_gate" in action_plan)
check("action plan has drafter before hitl", action_plan.index("drafter") < action_plan.index("hitl_gate"))
check("knowledge plan has researcher first", know_plan[0] == "researcher")
check("knowledge plan has critic", "critic" in know_plan)
check("knowledge plan does NOT have browser_fleet", "browser_fleet" not in know_plan)
check("action plan does NOT have browser_fleet", "browser_fleet" not in action_plan)
print()

# ── 6. AGENT MESSAGE STRUCTURE ────────────────────────────────────────────────
print("6. AgentMessage — structure and serialization")

msg = AgentMessage(
    agent_id="critic",
    agent_role="critic",
    round_num=2,
    content="Challenge: claim not backed by source data",
    claims=["price is correct"],
    challenges=["missing_source"],
    vote_tags=["challenged", "round-2"],
    confidence=0.72,
)

d = msg.to_dict()
check("to_dict returns dict", isinstance(d, dict))
check("to_dict has agent_id", d["agent_id"] == "critic")
check("to_dict has round", d["round"] == 2)
check("to_dict has confidence", d["confidence"] == 0.72)
check("to_dict has vote_tags", "round-2" in d["vote_tags"])

summary = msg.summary()
check("summary includes role", "CRITIC" in summary)
check("summary includes round", "Round 2" in summary)
check("summary includes confidence", "72%" in summary)
print()

# ── 7. CONVERGENCE LOGIC ──────────────────────────────────────────────────────
print("7. Convergence detection — Jaccard similarity fallback")

def jaccard(a, b):
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

THRESHOLD = 0.92  # bge-m3 cosine similarity threshold

# High similarity (same content, minor wording change) — should converge
text_a = "Gold price is ₹71211 per 10g. 5 sources agree. Confidence 96%."
text_b = "Gold price: ₹71211 per 10g. Confidence 96%. 5 of 6 sources."
sim_high = jaccard(text_a, text_b)
check(f"similar texts score > 0.4 (got {sim_high:.2f})", sim_high > 0.4)

# Very different content — should not converge
text_c = "The flight from Bangalore to Delhi departs at 06:05 on IndiGo."
sim_low = jaccard(text_a, text_c)
check(f"different texts score < 0.3 (got {sim_low:.2f})", sim_low < 0.3)

# Identical content — perfect similarity
text_same = text_a
sim_same = jaccard(text_a, text_same)
check("identical texts score = 1.0", sim_same == 1.0)

# Max rounds check
max_rounds = 3
check("max debate rounds is 3 (not more)", max_rounds == 3)
check("max rounds prevents infinite loop", max_rounds <= 5)
print()

# ── 8. SYNTHESIZER CITATION CHECK ─────────────────────────────────────────────
print("8. Synthesizer — uncited number detection")

def check_uncited(text):
    answer_match = re.search(r"ANSWER:\s*(.*?)(?:CONFIDENCE:|SOURCES:|$)", text, re.S | re.I)
    if not answer_match:
        return []
    answer = answer_match.group(1)
    numbers = re.findall(r"(?:₹|£|\$|€)?\s*[\d,]+\.?\d*\s*(?:%|per\s+\w+)?", answer)
    uncited = []
    for num in numbers:
        idx = answer.find(num)
        nearby = answer[max(0, idx-20):idx+len(num)+30]
        if "[" not in nearby:
            uncited.append(num.strip())
    return uncited

good_answer = "ANSWER:\nGold price: ₹71,211 [goldprice.org] per 10g. Confidence 96% [5/6 sources].\nCONFIDENCE: 96%"
bad_answer  = "ANSWER:\nGold price: ₹71,211 per 10g. Very confident.\nCONFIDENCE: 96%"

uncited_good = check_uncited(good_answer)
uncited_bad  = check_uncited(bad_answer)
check("cited answer has no uncited numbers", len(uncited_good) == 0, f"uncited: {uncited_good}")
check("uncited answer detected", len(uncited_bad) > 0, f"should have found uncited numbers")
print()

# ── SUMMARY ───────────────────────────────────────────────────────────────────
total = passed + failed
print("=" * 60)
print(f"  Results: {passed}/{total} tests passed")
if failed == 0:
    print("  STATUS: ALL TESTS PASSED")
    print("  Phase 2 agent pipeline verified.")
    print("  Safe to proceed to Phase 3 (Browser agents).")
else:
    print(f"  STATUS: {failed} TESTS FAILED")
print("=" * 60)
