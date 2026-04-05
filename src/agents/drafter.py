"""
Nexus AI — Smart Email Drafter v2
Handles two cases:
  1. KNOWN contact  → reads past emails → matches tone exactly
  2. UNKNOWN contact (no prior emails) → drafts professional cold email
                                         using context clues about the recipient

Security: read-only Gmail access, HITL gate always required before sending,
           PII masking on all drafts before Telegram preview.
"""
from __future__ import annotations
import json, re
from typing import Any, Optional
import structlog
from config.settings import get_settings
from src.agents.base import BaseAgent, AgentMessage, MessageBoard
from src.agents.llm_client import LLMClient

log = structlog.get_logger(__name__)
_s = get_settings()


class DrafterAgent(BaseAgent):
    AGENT_ID = "drafter"
    ROLE     = "drafter"
    TOOLS    = ["gmail_read"]   # read-only — cannot send

    # ── System prompts ─────────────────────────────────────────────────────────
    _TONE_SYSTEM = """You are an expert email communication analyst.
Analyse these past emails and extract the writer's communication DNA:
- Formality level (formal / semi-formal / casual)
- Typical greeting (Hi X, Dear X, Hello X, Hey X)
- Typical sign-off (Best, Regards, Thanks, Cheers, Warm regards)
- Average sentence length (short <10 words / medium 10-20 / long 20+)
- Vocabulary style (simple/direct OR corporate/formal OR friendly/warm)
- Any signature line

Return ONLY valid JSON:
{
  "formality": "semi-formal",
  "greeting": "Hi",
  "signoff": "Best",
  "sentence_length": "medium",
  "vocab_style": "friendly and direct",
  "signature": "Arjun Kumar | Product Lead",
  "tone_notes": "brief description",
  "emails_analysed": 5
}"""

    _KNOWN_DRAFT_SYSTEM = """You are composing an email on behalf of the user.
You must match their writing style EXACTLY based on the tone profile provided.
The email must sound like the user wrote it personally — not like AI.

Rules:
- Match the greeting and sign-off exactly
- Match sentence length preference
- No filler phrases ("I hope this email finds you well", "As per our conversation")
- No AI giveaways ("Certainly!", "Of course!", "I'd be happy to")
- Be direct and clear
- Include ONLY what the user asked for

Output format (EXACT):
SUBJECT: <subject line>
BODY:
<email body ending with sign-off and signature>"""

    _COLD_DRAFT_SYSTEM = """You are composing a professional first-contact email on behalf of the user.
The user has NEVER emailed this person before. Write a professional, warm, and direct email.

Principles for a great cold email:
1. Subject: specific, benefit-clear, not click-bait (max 8 words)
2. Opening: no "Hope you're well" — start with the point or a relevant hook
3. Body: 3–4 short paragraphs max, each with one clear purpose
4. Value: what's in it for the recipient? Make it explicit
5. CTA: one clear, low-friction ask (a 20-min call, a reply, a quick question)
6. Sign-off: professional (Best regards / Kind regards / Best)
7. Tone: warm, confident, NOT salesy or desperate

Rules:
- No "I wanted to reach out" — just reach out
- No "Please do not hesitate to contact me"
- No "I am writing to" — start with the substance
- Respect their time — keep it under 150 words in the body

Output format (EXACT):
SUBJECT: <subject line>
BODY:
<email body>"""

    _CONTEXT_SYSTEM = """You are extracting email addressing information from a user request.
Extract:
- recipient_name: full name if mentioned, else empty string
- recipient_email: email address if mentioned, else empty string  
- recipient_role: job title/role if mentioned (e.g. "manager", "CTO", "professor")
- recipient_company: company if mentioned
- purpose: what the email is about (1 sentence)
- key_points: list of specific points to include
- urgency: "normal" | "urgent" | "low"
- context_clues: any other info about the recipient (LinkedIn, mutual connection, etc.)

Return ONLY valid JSON."""

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        super().__init__()
        from src.agents.llm_client import llm_client
        self._llm = llm or llm_client

    async def run(
        self, task_id: str, query: str,
        board: MessageBoard, context: dict[str, Any], round_num: int = 0,
    ) -> AgentMessage:
        self.use_tool("gmail_read")

        # Step 1: Extract context from the user's request
        email_ctx = await self._extract_context(query, context)
        recipient = (email_ctx.get("recipient_email","")
                     or email_ctx.get("recipient_name","")
                     or context.get("recipient",""))

        # Step 2: Try to fetch past emails
        past_emails = await self._fetch_past_emails(recipient, context)
        has_history = len(past_emails) >= 2

        # Step 3: Draft accordingly
        if has_history:
            tone_profile = await self._extract_tone(past_emails)
            draft = await self._draft_with_tone(query, email_ctx, tone_profile)
            mode = f"tone-matched ({len(past_emails)} past emails)"
            confidence = 0.93
        else:
            draft = await self._draft_cold(query, email_ctx)
            mode = "professional cold email (no prior contact found)"
            confidence = 0.85

        # Step 4: Parse subject + body
        subject, body = self._parse_draft(draft)

        # Step 5: Build structured output
        structured = {
            "to":       recipient,
            "to_name":  email_ctx.get("recipient_name",""),
            "to_role":  email_ctx.get("recipient_role",""),
            "subject":  subject,
            "body":     body,
            "mode":     mode,
            "purpose":  email_ctx.get("purpose",""),
        }

        log.info("drafter_complete", task_id=task_id[:8],
                 recipient=recipient[:30], mode=mode, has_history=has_history)

        return self.make_message(
            content=json.dumps(structured),
            round_num=round_num,
            vote_tags=["draft-ready", mode.split("(")[0].strip(), f"confidence={confidence:.0%}"],
            confidence=confidence,
        )

    # ── Context extraction ─────────────────────────────────────────────────────
    async def _extract_context(self, query: str, context: dict) -> dict:
        """Use LLM to parse recipient info and purpose from the user's request."""
        # Fast regex path first
        email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", query)
        name_match  = re.search(
            r"\b(?:to|for|email|mail|message|contact|write to)\s+([A-Z][a-z]+(?: [A-Z][a-z]+)*)", query
        )
        quick = {
            "recipient_email": email_match.group() if email_match else "",
            "recipient_name":  name_match.group(1) if name_match else "",
        }
        # LLM for richer extraction
        try:
            r = await self._llm.chat(
                model=_s.groq_fast_model,
                messages=[{"role":"user","content":
                    f"Extract email info from: {query}\n"
                    f"Context: {json.dumps(context.get('extra',{}))}\n"
                    f"Return JSON."}],
                system=self._CONTEXT_SYSTEM,
                temperature=0, max_tokens=256, json_mode=True,
            )
            parsed = json.loads(r.content)
            # Merge, fast regex takes priority for clear matches
            if quick["recipient_email"]:
                parsed["recipient_email"] = quick["recipient_email"]
            if quick["recipient_name"]:
                parsed["recipient_name"] = quick["recipient_name"]
            return parsed
        except Exception:
            return {**quick, "purpose": query, "key_points": [], "urgency": "normal"}

    # ── Email history ──────────────────────────────────────────────────────────
    async def _fetch_past_emails(self, recipient: str, context: dict) -> list[dict]:
        """Fetch real emails via Gmail API. Returns empty list if not configured."""
        from src.tools.email_tool import email_tool
        if not recipient:
            return []
        try:
            # Search for emails to/from this recipient
            emails = await email_tool.read_recent(count=20, query=f"to:{recipient} OR from:{recipient}")
            if not emails:
                # Try by name if we have it
                name = context.get("recipient_name","")
                if name:
                    emails = await email_tool.read_recent(count=10, query=name)
            return emails
        except Exception as e:
            log.debug("email_history_unavailable", error=str(e))
            return []

    # ── Tone extraction ────────────────────────────────────────────────────────
    async def _extract_tone(self, emails: list[dict]) -> dict:
        if not emails:
            return {"formality":"semi-formal","greeting":"Hi","signoff":"Best",
                    "sentence_length":"medium","vocab_style":"direct","signature":""}
        sample = "\n\n---\n\n".join(
            f"From: {e.get('from','')}\nSubject: {e.get('subject','')}\n{e.get('snippet','')[:300]}"
            for e in emails[:6]
        )
        try:
            r = await self._llm.chat(
                model=_s.groq_fast_model,
                messages=[{"role":"user","content":f"Analyse these emails:\n\n{sample}"}],
                system=self._TONE_SYSTEM, temperature=0.0,
                max_tokens=300, json_mode=True, cache_ttl=3600,
            )
            return json.loads(r.content)
        except Exception:
            return {"formality":"semi-formal","greeting":"Hi","signoff":"Best",
                    "sentence_length":"medium","vocab_style":"direct","signature":""}

    # ── Draft with tone (known contact) ───────────────────────────────────────
    async def _draft_with_tone(self, query: str, ctx: dict, tone: dict) -> str:
        tone_desc = (
            f"Greeting: {tone.get('greeting','Hi')}\n"
            f"Sign-off: {tone.get('signoff','Best')}\n"
            f"Formality: {tone.get('formality','semi-formal')}\n"
            f"Sentence length: {tone.get('sentence_length','medium')}\n"
            f"Vocabulary: {tone.get('vocab_style','direct')}\n"
            f"Signature: {tone.get('signature','')}"
        )
        prompt = (
            f"Email request: {query}\n\n"
            f"Recipient: {ctx.get('recipient_name','')} {ctx.get('recipient_role','')}\n"
            f"Purpose: {ctx.get('purpose','')}\n"
            f"Key points: {', '.join(ctx.get('key_points',[]))}\n\n"
            f"TONE PROFILE (match exactly):\n{tone_desc}"
        )
        r = await self._llm.chat(
            model=_s.groq_primary_model,
            messages=[{"role":"user","content":prompt}],
            system=self._KNOWN_DRAFT_SYSTEM, temperature=0.25, max_tokens=600,
        )
        return r.content

    # ── Cold email draft (unknown contact) ────────────────────────────────────
    async def _draft_cold(self, query: str, ctx: dict) -> str:
        """
        Professional cold email for first contact.
        Uses context clues: recipient role, company, purpose, mutual connections.
        """
        context_str = (
            f"Recipient: {ctx.get('recipient_name','')} "
            f"({ctx.get('recipient_role','')} at {ctx.get('recipient_company','')})\n"
            f"Purpose: {ctx.get('purpose','')}\n"
            f"Key points to include: {', '.join(ctx.get('key_points',[]))}\n"
            f"Context clues: {ctx.get('context_clues','no prior relationship')}\n"
            f"Urgency: {ctx.get('urgency','normal')}\n\n"
            f"Original request: {query}"
        )
        r = await self._llm.chat(
            model=_s.groq_primary_model,
            messages=[{"role":"user","content":context_str}],
            system=self._COLD_DRAFT_SYSTEM, temperature=0.3, max_tokens=600,
        )
        return r.content

    # ── Parse SUBJECT / BODY ──────────────────────────────────────────────────
    @staticmethod
    def _parse_draft(text: str) -> tuple[str, str]:
        subj_m = re.search(r"SUBJECT:\s*(.+?)(?:\n|$)", text, re.I)
        body_m  = re.search(r"BODY:\s*\n(.*)", text, re.I | re.S)
        subject = subj_m.group(1).strip() if subj_m else "Follow up"
        body    = body_m.group(1).strip()  if body_m  else text.strip()
        return subject, body
