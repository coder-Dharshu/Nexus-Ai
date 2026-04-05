"""
Nexus AI — Email Intelligence Engine
Reads ALL inbox emails, analyses tone across every sender,
categorises by importance, learns patterns, groups threads.
Runs on Gmail API (free OAuth).
"""
from __future__ import annotations
import asyncio, json, re, time
from dataclasses import dataclass, field
from typing import Optional
import structlog

log = structlog.get_logger(__name__)


@dataclass
class ToneProfile:
    sender: str
    formality: str          # formal | semi-formal | casual
    avg_response_time_h: float
    typical_greeting: str
    typical_signoff: str
    vocab_style: str
    sentence_length: str    # short | medium | long
    emotional_tone: str     # warm | neutral | terse | urgent
    sample_phrases: list[str]
    emails_analysed: int


@dataclass
class EmailCategory:
    name: str               # e.g. "Urgent", "Finance", "Team", "Newsletter"
    color: str              # for UI display
    rules: list[str]        # plain-English rules
    email_ids: list[str]
    unread_count: int = 0


@dataclass
class InboxSummary:
    total_emails: int
    unread_count: int
    categories: list[EmailCategory]
    tone_profiles: list[ToneProfile]
    action_required: list[dict]   # emails needing a reply
    digest: str                   # one-paragraph summary for Telegram


# ── Category definitions ───────────────────────────────────────────────────────
CATEGORY_RULES = [
    {
        "name": "Urgent",
        "color": "#E24B4A",
        "keywords": ["urgent","asap","immediately","deadline","critical","action required",
                     "URGENT","by today","by eod","time sensitive","overdue"],
        "sender_patterns": [],
        "subject_patterns": [r"\b(urgent|asap|deadline|action required)\b"],
    },
    {
        "name": "Finance",
        "color": "#BA7517",
        "keywords": ["invoice","payment","salary","reimbursement","expense","receipt",
                     "bank","transaction","tax","gst","tds","payroll","billing"],
        "sender_patterns": [r"(finance|accounts|billing|payments)@",
                            r"(noreply|no-reply)@.*bank",
                            r"(hdfc|icici|sbi|axis|kotak)"],
        "subject_patterns": [r"\b(invoice|payment|salary|receipt)\b"],
    },
    {
        "name": "Work / Team",
        "color": "#534AB7",
        "keywords": ["standup","sprint","jira","confluence","pr review","pull request",
                     "deployment","release","meeting","sync","project","milestone"],
        "sender_patterns": [r"@(your-company|company|corp|inc|ltd)\."],
        "subject_patterns": [r"\b(re:|fwd:|meeting|sync|review|standup)\b"],
    },
    {
        "name": "HR / Admin",
        "color": "#1D9E75",
        "keywords": ["leave","attendance","policy","holiday","appraisal","performance",
                     "onboarding","offer letter","payslip","benefits"],
        "sender_patterns": [r"(hr|human.resources|people.ops)@"],
        "subject_patterns": [r"\b(leave|holiday|appraisal|offer|payslip)\b"],
    },
    {
        "name": "Newsletters",
        "color": "#888780",
        "keywords": ["unsubscribe","view in browser","this email was sent to",
                     "update your preferences","weekly digest","newsletter"],
        "sender_patterns": [r"(noreply|no-reply|newsletter|digest|updates)@",
                            r"(substack|mailchimp|sendgrid|klaviyo)"],
        "subject_patterns": [],
    },
    {
        "name": "Social",
        "color": "#D4537E",
        "keywords": ["linkedin","twitter","instagram","facebook","github",
                     "notion","slack","discord","zoom"],
        "sender_patterns": [r"@(linkedin|twitter|instagram|facebook|github|notion)\."],
        "subject_patterns": [],
    },
    {
        "name": "Personal",
        "color": "#5DCAA5",
        "keywords": [],
        "sender_patterns": [r"@(gmail|yahoo|hotmail|outlook)\."],
        "subject_patterns": [],
    },
]


class EmailIntelligence:
    """
    Full inbox analysis: read all emails, categorise, analyse tone, detect action items.
    """

    def __init__(self):
        self._gmail = None

    def _svc(self):
        if self._gmail: return self._gmail
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            from src.security.keychain import secrets_manager
            from config.settings import get_settings
            raw = secrets_manager.get(get_settings().gmail_credentials_key, required=True)
            creds = Credentials.from_authorized_user_info(json.loads(raw))
            self._gmail = build("gmail", "v1", credentials=creds,
                                cache_discovery=False)
            return self._gmail
        except Exception as e:
            raise RuntimeError(f"Gmail not configured: {e}. Run: nexus setup → Gmail OAuth")

    # ── Read emails ────────────────────────────────────────────────────────────

    async def read_inbox(self, max_emails: int = 200) -> list[dict]:
        """Read up to max_emails from inbox with full metadata."""
        def _sync():
            svc = self._svc()
            result = svc.users().messages().list(
                userId="me", q="in:inbox", maxResults=max_emails).execute()
            msgs = []
            for item in result.get("messages", [])[:max_emails]:
                try:
                    msg = svc.users().messages().get(
                        userId="me", id=item["id"],
                        format="full",
                        metadataHeaders=["From","To","Subject","Date","Message-ID",
                                         "In-Reply-To","Cc"]).execute()
                    headers = {h["name"]:h["value"]
                               for h in msg.get("payload",{}).get("headers",[])}
                    body = self._extract_body(msg.get("payload", {}))
                    msgs.append({
                        "id":      item["id"],
                        "thread":  msg.get("threadId",""),
                        "from":    headers.get("From",""),
                        "to":      headers.get("To",""),
                        "subject": headers.get("Subject",""),
                        "date":    headers.get("Date",""),
                        "snippet": msg.get("snippet",""),
                        "body":    body[:1000],
                        "labels":  msg.get("labelIds",[]),
                        "unread":  "UNREAD" in msg.get("labelIds",[]),
                        "is_reply": bool(headers.get("In-Reply-To","")),
                    })
                except Exception as e:
                    log.warning("msg_read_failed", id=item["id"][:8], error=str(e)[:60])
            return msgs
        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            log.error("inbox_read_failed", error=str(e))
            return []

    def _extract_body(self, payload: dict) -> str:
        """Recursively extract plain text body from Gmail payload."""
        import base64
        if payload.get("mimeType") == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        for part in payload.get("parts", []):
            text = self._extract_body(part)
            if text:
                return text
        return ""

    # ── Categorise ─────────────────────────────────────────────────────────────

    def categorise_emails(self, emails: list[dict]) -> list[EmailCategory]:
        """Rule-based categorisation. Fast, no LLM needed."""
        cats: dict[str, EmailCategory] = {}
        for rule in CATEGORY_RULES:
            cats[rule["name"]] = EmailCategory(
                name=rule["name"], color=rule["color"],
                rules=rule["keywords"][:3], email_ids=[], unread_count=0)

        uncategorised = []
        for email in emails:
            text = (email["subject"] + " " + email["from"] + " " +
                    email["snippet"]).lower()
            matched = False
            for rule in CATEGORY_RULES:
                # Keyword match
                if any(kw.lower() in text for kw in rule["keywords"]):
                    cats[rule["name"]].email_ids.append(email["id"])
                    if email["unread"]:
                        cats[rule["name"]].unread_count += 1
                    matched = True; break
                # Sender domain match
                sender = email["from"].lower()
                if any(re.search(p, sender) for p in rule["sender_patterns"]):
                    cats[rule["name"]].email_ids.append(email["id"])
                    if email["unread"]:
                        cats[rule["name"]].unread_count += 1
                    matched = True; break
            if not matched:
                uncategorised.append(email["id"])

        if uncategorised:
            cats["Other"] = EmailCategory(name="Other", color="#D3D1C7",
                                          rules=[], email_ids=uncategorised,
                                          unread_count=sum(1 for e in emails
                                                           if e["id"] in uncategorised
                                                           and e["unread"]))

        return [c for c in cats.values() if c.email_ids]

    # ── Tone analysis ──────────────────────────────────────────────────────────

    async def analyse_tones(self, emails: list[dict],
                             llm_client=None) -> list[ToneProfile]:
        """
        Group emails by sender, analyse tone pattern per sender.
        Uses Groq for nuanced tone extraction.
        """
        from collections import defaultdict
        by_sender: dict[str, list[dict]] = defaultdict(list)
        for e in emails:
            sender_email = re.search(r"<(.+?)>", e["from"])
            key = sender_email.group(1) if sender_email else e["from"]
            by_sender[key].append(e)

        profiles = []
        # Analyse senders who sent 3+ emails (enough signal)
        significant = {k: v for k, v in by_sender.items() if len(v) >= 3}

        if not llm_client or not significant:
            return profiles

        # Batch analyse up to 20 senders to control Groq usage
        for sender, sender_emails in list(significant.items())[:20]:
            try:
                sample = "\n---\n".join(
                    f"Subject: {e['subject']}\n{e['body'][:300]}"
                    for e in sender_emails[:5]
                )
                r = await llm_client.chat(
                    model="groq-fast",
                    messages=[{"role":"user","content":
                        f"Analyse the tone and communication style of this sender.\n\n"
                        f"Sender: {sender}\n\nEmails:\n{sample}\n\n"
                        f"Return ONLY valid JSON:\n"
                        f'{{"formality":"semi-formal","typical_greeting":"Hi","typical_signoff":"Best",'
                        f'"vocab_style":"direct","sentence_length":"medium",'
                        f'"emotional_tone":"warm","sample_phrases":["phrase1","phrase2"]}}'}],
                    temperature=0, max_tokens=200, json_mode=True, cache_ttl=3600,
                )
                d = json.loads(r.content)
                profiles.append(ToneProfile(
                    sender=sender,
                    formality=d.get("formality","unknown"),
                    avg_response_time_h=0,  # computed separately
                    typical_greeting=d.get("typical_greeting",""),
                    typical_signoff=d.get("typical_signoff",""),
                    vocab_style=d.get("vocab_style",""),
                    sentence_length=d.get("sentence_length","medium"),
                    emotional_tone=d.get("emotional_tone","neutral"),
                    sample_phrases=d.get("sample_phrases",[]),
                    emails_analysed=len(sender_emails),
                ))
            except Exception as e:
                log.warning("tone_analysis_failed", sender=sender[:30], error=str(e)[:60])

        return profiles

    # ── Action items ───────────────────────────────────────────────────────────

    def find_action_required(self, emails: list[dict]) -> list[dict]:
        """
        Find emails that need a reply or action.
        Detects: direct questions, requests, deadlines, meeting invites.
        """
        ACTION_SIGNALS = [
            r"\?",                                    # any question
            r"\b(please|kindly|can you|could you)\b", # requests
            r"\b(by|before|deadline|due|asap)\b",     # deadlines
            r"\b(confirm|confirming|rsvp|accept|decline)\b",  # invites
            r"\b(let me know|awaiting|waiting for)\b", # follow-ups
            r"\b(action|required|needed|must)\b",
        ]
        SKIP_PATTERNS = [
            r"unsubscribe", r"newsletter", r"noreply", r"no-reply",
            r"notification", r"alert", r"reminder",
        ]

        actions = []
        for email in emails:
            if not email["unread"]:
                continue  # only unread

            text = (email["subject"] + " " + email["snippet"]).lower()
            from_addr = email["from"].lower()

            # Skip automated/newsletter emails
            if any(re.search(p, from_addr) or re.search(p, text)
                   for p in SKIP_PATTERNS):
                continue

            # Check for action signals
            score = sum(1 for p in ACTION_SIGNALS if re.search(p, text, re.I))
            if score >= 2:
                actions.append({
                    "id":      email["id"],
                    "from":    email["from"],
                    "subject": email["subject"],
                    "snippet": email["snippet"][:120],
                    "date":    email["date"],
                    "score":   score,
                })

        return sorted(actions, key=lambda x: x["score"], reverse=True)[:10]

    # ── Full analysis ──────────────────────────────────────────────────────────

    async def full_analysis(self, llm_client=None) -> InboxSummary:
        """Run complete inbox analysis: read → categorise → tone → action items."""
        log.info("email_analysis_start")

        emails = await self.read_inbox(max_emails=200)
        if not emails:
            return InboxSummary(
                total_emails=0, unread_count=0, categories=[],
                tone_profiles=[], action_required=[],
                digest="Gmail not connected or inbox is empty.",
            )

        unread = sum(1 for e in emails if e["unread"])
        log.info("email_analysis_read", total=len(emails), unread=unread)

        # Run categorisation and tone analysis in parallel
        categories, tones, actions = await asyncio.gather(
            asyncio.to_thread(self.categorise_emails, emails),
            self.analyse_tones(emails, llm_client),
            asyncio.to_thread(self.find_action_required, emails),
        )

        # Build digest via LLM
        digest = await self._build_digest(emails, categories, actions, llm_client)

        return InboxSummary(
            total_emails=len(emails),
            unread_count=unread,
            categories=categories,
            tone_profiles=tones,
            action_required=actions,
            digest=digest,
        )

    async def _build_digest(self, emails, categories, actions, llm_client) -> str:
        if not llm_client:
            return (f"Inbox: {len(emails)} emails, "
                    f"{sum(1 for e in emails if e['unread'])} unread, "
                    f"{len(actions)} need replies.")
        try:
            cat_summary = ", ".join(
                f"{c.name}: {len(c.email_ids)} ({c.unread_count} unread)"
                for c in categories
            )
            action_summary = "\n".join(
                f"- From {a['from'][:40]}: {a['subject'][:60]}"
                for a in actions[:5]
            )
            r = await llm_client.chat(
                model="groq-fast",
                messages=[{"role":"user","content":
                    f"Summarise this inbox in 2-3 sentences for a busy professional.\n"
                    f"Categories: {cat_summary}\n"
                    f"Emails needing replies:\n{action_summary or 'None'}\n"
                    f"Write a concise natural-language digest."}],
                temperature=0.2, max_tokens=120,
            )
            return r.content.strip()
        except Exception as e:
            log.warning("digest_build_failed", error=str(e))
            return "Inbox analysed — see categories below."

    # ── Get tone for one sender (used by drafter) ──────────────────────────────

    async def get_sender_tone(self, sender_email: str,
                               llm_client=None) -> Optional[ToneProfile]:
        """Get tone profile for a specific sender to match when drafting."""
        emails = await self.read_inbox(max_emails=50)
        sender_emails = [
            e for e in emails
            if sender_email.lower() in e["from"].lower()
        ]
        if not sender_emails:
            return None
        profiles = await self.analyse_tones(sender_emails, llm_client)
        return profiles[0] if profiles else None


email_intelligence = EmailIntelligence()
