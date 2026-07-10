"""
leads.py — Voxniac ONE Phase 4 Etapa C: the Campaigns lead store.

SQLite (stdlib `sqlite3`, no ORM, no external DB dependency — sovereignty
principle) persisted to leads.db next to this file (gitignored — see
.gitignore). Schema is a Python port of neura-sales's lead model
(`src/lib/types.ts` Lead interface / `src/lib/db.ts`'s CREATE TABLE), trimmed
to what this codebase actually uses (no contactAvatar/companyLogo/linkedinUrl
— this UI never renders avatars, per PLAN_UI_CONTROL_ROOM.md's "poda" rule of
never shipping a field with zero real behavior behind it).

Non-negotiable ethical decision from the CEO (innegociable — see
PLAN_FASE4_CAMPAIGNS.md's Etapa C): a lead's real phone number and full email
are NEVER persisted anywhere, not even transiently in a log line. Only
mask_phone()/mask_email()'s OUTPUT ever reaches the leads table — the raw
value read from an imported CSV row is discarded the instant it's been
masked, inside the same expression, and is never assigned to a variable that
outlives that line.

  phone:  "+13055551234"        -> "+1305•••1234"   (prefix 5 chars incl. "+",
                                                       "•••", last 4 digits)
  email:  "jane@acme.com"       -> "acme.com"        (domain only)

Bulletproof by the same house rules as vz_config.py: a missing/corrupt
leads.db is recreated on next use (CREATE TABLE IF NOT EXISTS run at the top
of every public function that opens a connection); a malformed CSV row is
skipped, never a crash.
"""

import csv
import io
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from vz_config import FIREWORKS_API_KEY  # noqa: F401 (imported for callers that check availability)
from vz_llm import stream_chat

logger = logging.getLogger("voxniac_one.leads")

THIS_DIR = Path(__file__).resolve().parent
LEADS_DB_PATH = THIS_DIR / "leads.db"

STATUSES = ("COLD", "WARM", "HOT")
DEFAULT_STATUS = "COLD"

# Fields a PATCH /leads/{id} may change. Deliberately excludes id/phone/email:
# phone and email are obfuscated once, at import time, from a source (the
# CSV) this app doesn't keep — there is no "real" value anywhere to re-derive
# an edit from, so editing them here would just be typing fiction.
EDITABLE_FIELDS = (
    "contactName",
    "companyName",
    "status",
    "isBallena",
    "industry",
    "companySize",
    "seniority",
    "painPoints",
    "customFirstMessage",
    "customSystemPrompt",
)

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)

# Same model/persona family as interviewer.py's plan drafter (Phase 3 promptizer)
# — gpt-oss-120b via Fireworks. reasoning_effort "low" (not "high" like the
# onboarding interviewer): this is a short, single-lead personalization task,
# not a full business-onboarding draft, so the interviewer's ~60s timeout
# budget for "high" reasoning would be needless latency for a UI button click.
LEAD_LLM_MODEL_ID = "accounts/fireworks/models/gpt-oss-120b"
LEAD_PROMPT_REASONING_EFFORT = "low"
LEAD_PROMPT_MAX_TOKENS = 900
LEAD_PROMPT_TIMEOUT_S = 30

CLASSIFY_REASONING_EFFORT = "low"
CLASSIFY_MAX_TOKENS = 400
CLASSIFY_TIMEOUT_S = 30


class LeadsError(Exception):
    """Raised for lead-store failures that must be surfaced to the client as
    a fail-loud HTTP error (server.py's job) — mirrors interviewer.py's
    InterviewerError philosophy: never silently swallow, never crash the
    server process."""


# ---------------------------------------------------------------------------
# Obfuscation (Gate 2 / CEO's ethical decision — see module docstring)
# ---------------------------------------------------------------------------
def mask_phone(raw_phone: "str | None") -> str:
    """+13055551234 -> +1305•••1234. Bulletproof: too-short/garbage input
    never raises, it just degrades to a fully-masked "•••" or "" for empty
    input. The `raw_phone` argument itself is never returned, logged, or
    stored anywhere by this function or any caller in this module."""
    if not raw_phone:
        return ""
    only = re.sub(r"[^\d+]", "", raw_phone)
    if not only.replace("+", ""):
        return ""
    if not only.startswith("+"):
        only = "+" + only
    digits_only = re.sub(r"\D", "", only)
    if len(digits_only) < 4:
        return "•••"
    if len(only) < 9:
        # Not enough length for a meaningful 5-char prefix + last 4 without
        # the two overlapping — mask everything except the last 4 digits.
        return f"•••{digits_only[-4:]}"
    prefix = only[:5]
    last4 = digits_only[-4:]
    return f"{prefix}•••{last4}"


def mask_email(raw_email: "str | None") -> str:
    """jane@acme.com -> acme.com. Never returns the local part. Bulletproof:
    malformed input (no "@") returns ''."""
    if not raw_email or "@" not in raw_email:
        return ""
    domain = raw_email.strip().split("@", 1)[1].strip()
    return domain


# ---------------------------------------------------------------------------
# Connection + schema (bulletproof: CREATE TABLE IF NOT EXISTS every open)
# ---------------------------------------------------------------------------
def _get_conn(path: "Path | None" = None) -> sqlite3.Connection:
    db_path = path or LEADS_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            contactName TEXT NOT NULL DEFAULT '',
            companyName TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'COLD',
            isBallena INTEGER NOT NULL DEFAULT 0,
            industry TEXT NOT NULL DEFAULT '',
            companySize TEXT NOT NULL DEFAULT '',
            seniority TEXT NOT NULL DEFAULT '',
            painPoints TEXT NOT NULL DEFAULT '[]',
            customFirstMessage TEXT NOT NULL DEFAULT '',
            customSystemPrompt TEXT NOT NULL DEFAULT '',
            lastCallDate TEXT,
            lastCallId TEXT,
            classificationReasoning TEXT NOT NULL DEFAULT '',
            createdAt TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["isBallena"] = bool(d.get("isBallena"))
    try:
        pain_points = json.loads(d.get("painPoints") or "[]")
        if not isinstance(pain_points, list):
            pain_points = [str(pain_points)]
    except (json.JSONDecodeError, TypeError):
        pain_points = []
    d["painPoints"] = pain_points
    return d


# ---------------------------------------------------------------------------
# CSV import (Apollo export format, tolerant of missing/renamed columns)
# ---------------------------------------------------------------------------
# Candidate column names (checked case-insensitively, whitespace-stripped)
# for each field this app cares about. Apollo's own export sometimes varies
# ("Work Direct Phone" vs "Mobile Phone" vs plain "Phone"), and a hand-edited
# CSV (e.g. the demo fixture) may use even simpler headers — every candidate
# is tried in order, first non-empty match wins.
_CONTACT_FIRST_KEYS = ("first name", "firstname")
_CONTACT_LAST_KEYS = ("last name", "lastname")
_COMPANY_KEYS = ("company", "company name", "organization")
_EMAIL_KEYS = ("email",)
_PHONE_KEYS = ("work direct phone", "mobile phone", "phone", "direct phone", "corporate phone")
_INDUSTRY_KEYS = ("industry",)
_COMPANY_SIZE_KEYS = ("# employees", "employees", "company size", "employee count")
_SENIORITY_KEYS = ("seniority", "title")


def _pick(normalized_row: dict, *candidate_keys: str) -> str:
    for key in candidate_keys:
        value = normalized_row.get(key)
        if value:
            return value
    return ""


def import_csv(file_bytes: bytes, path: "Path | None" = None) -> dict:
    """
    Parses an Apollo-export-shaped CSV (tolerant of missing/renamed columns —
    see the _*_KEYS candidate lists above) and inserts one lead per usable
    row. Phone/email are masked HERE, in the same expression they're read
    from the row — the raw values are never assigned to a variable, logged,
    or written anywhere else.

    A row is skipped (counted in "skipped", never raises) when it has
    neither a contact name (first+last) nor a company name — i.e. it carries
    no identifiable lead at all (a common CSV export artifact: trailing
    blank lines, a stray header repeated mid-file, etc.).

    Returns {"imported": int, "skipped": int}.
    """
    try:
        text = file_bytes.decode("utf-8-sig", errors="replace")
    except Exception as exc:  # pragma: no cover - decode() with errors="replace" never raises
        raise LeadsError(f"Could not decode CSV upload as text: {exc}") from exc

    try:
        reader = csv.DictReader(io.StringIO(text))
    except csv.Error as exc:
        raise LeadsError(f"Could not parse CSV: {exc}") from exc

    imported = 0
    skipped = 0
    conn = _get_conn(path)
    try:
        for row in reader:
            if row is None:
                skipped += 1
                continue
            normalized = {
                (k or "").strip().lower(): (v or "").strip()
                for k, v in row.items()
                if k is not None
            }
            first = _pick(normalized, *_CONTACT_FIRST_KEYS)
            last = _pick(normalized, *_CONTACT_LAST_KEYS)
            contact_name = f"{first} {last}".strip()
            company_name = _pick(normalized, *_COMPANY_KEYS)

            if not contact_name and not company_name:
                skipped += 1
                continue

            lead = {
                "id": uuid.uuid4().hex[:12],
                "contactName": contact_name or "Unknown Contact",
                "companyName": company_name or "Unknown Company",
                # Masked in-line: the raw CSV value never touches another name.
                "phone": mask_phone(_pick(normalized, *_PHONE_KEYS)),
                "email": mask_email(_pick(normalized, *_EMAIL_KEYS)),
                "status": DEFAULT_STATUS,
                "isBallena": 0,
                "industry": _pick(normalized, *_INDUSTRY_KEYS),
                "companySize": _pick(normalized, *_COMPANY_SIZE_KEYS),
                "seniority": _pick(normalized, *_SENIORITY_KEYS),
                "painPoints": "[]",
                "customFirstMessage": "",
                "customSystemPrompt": "",
                "lastCallDate": None,
                "lastCallId": None,
                "classificationReasoning": "",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            conn.execute(
                """
                INSERT INTO leads (
                    id, contactName, companyName, phone, email, status, isBallena,
                    industry, companySize, seniority, painPoints, customFirstMessage,
                    customSystemPrompt, lastCallDate, lastCallId, classificationReasoning,
                    createdAt
                ) VALUES (
                    :id, :contactName, :companyName, :phone, :email, :status, :isBallena,
                    :industry, :companySize, :seniority, :painPoints, :customFirstMessage,
                    :customSystemPrompt, :lastCallDate, :lastCallId, :classificationReasoning,
                    :createdAt
                )
                """,
                lead,
            )
            imported += 1
        conn.commit()
    finally:
        conn.close()

    return {"imported": imported, "skipped": skipped}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def list_leads(status: "str | None" = None, path: "Path | None" = None) -> list:
    conn = _get_conn(path)
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM leads WHERE status = ? ORDER BY createdAt DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY createdAt DESC").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_lead(lead_id: str, path: "Path | None" = None) -> "dict | None":
    conn = _get_conn(path)
    try:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def update_lead(lead_id: str, fields: dict, path: "Path | None" = None) -> "dict | None":
    """Applies a whitelisted subset of `fields` (see EDITABLE_FIELDS) to the
    lead. Unknown keys are silently ignored (not an error — the caller,
    server.py, is expected to validate types before calling this; this
    function's own job is just "never write outside the whitelist" and
    "never crash on a missing lead", not full request validation).
    Returns the updated lead dict, or None if lead_id doesn't exist."""
    updates = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS}
    if not updates:
        return get_lead(lead_id, path)

    conn = _get_conn(path)
    try:
        existing = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not existing:
            return None

        set_clauses = []
        params = {}
        for key, value in updates.items():
            if key == "isBallena":
                value = 1 if value else 0
            elif key == "painPoints":
                value = json.dumps(value if isinstance(value, list) else [])
            set_clauses.append(f"{key} = :{key}")
            params[key] = value
        params["id"] = lead_id

        conn.execute(f"UPDATE leads SET {', '.join(set_clauses)} WHERE id = :id", params)
        conn.commit()

        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def update_lead_generated_prompt(
    lead_id: str, first_message: str, system_prompt: str, path: "Path | None" = None
) -> "dict | None":
    return update_lead(
        lead_id,
        {"customFirstMessage": first_message, "customSystemPrompt": system_prompt},
        path,
    )


def update_lead_call(
    lead_id: str, call_id: str, call_date: "str | None" = None, path: "Path | None" = None
) -> "dict | None":
    conn = _get_conn(path)
    try:
        existing = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not existing:
            return None
        conn.execute(
            "UPDATE leads SET lastCallId = ?, lastCallDate = ? WHERE id = ?",
            (call_id, call_date or datetime.now(timezone.utc).isoformat(), lead_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def update_lead_classification(
    lead_id: str, status: str, reasoning: str, path: "Path | None" = None
) -> "dict | None":
    """Sets status + classificationReasoning together in one write (both are
    the direct output of one classify_lead_call() result — never applied
    separately, so a lead's status and its reasoning never disagree)."""
    if status not in STATUSES:
        raise LeadsError(f"Invalid status '{status}', must be one of {STATUSES}")

    conn = _get_conn(path)
    try:
        existing = conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not existing:
            return None
        conn.execute(
            "UPDATE leads SET status = ?, classificationReasoning = ? WHERE id = ?",
            (status, reasoning or "", lead_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def delete_lead(lead_id: str, path: "Path | None" = None) -> bool:
    conn = _get_conn(path)
    try:
        cur = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# LLM: per-lead prompt generation (port of neura-sales's generate-prompt
# route, adapted from Next.js+Gemini to this codebase's own Fireworks
# gpt-oss-120b client — see vz_llm.stream_chat, same call shape interviewer.py
# uses for its plan-drafter). Deliberately business-agnostic: instead of
# hardcoding neura-sales's own SingularityOS-specific pitch, this pulls the
# CURRENT agent's identity from agent_profile.json (whatever business the
# founder configured via the onboarding interviewer / Profile Editor) so a
# lead-specific script always matches the live agent's actual persona and
# guardrails instead of contradicting them.
# ---------------------------------------------------------------------------
class _SimpleHistory:
    """Minimal duck-type adapter so vz_llm.stream_chat can consume a single
    user message with an explicit system_prompt override — mirrors
    interviewer.py's _HistoryView."""

    def __init__(self, user_text: str):
        self._messages = [{"role": "user", "content": user_text}]

    def as_messages_with_system(self, system_prompt=None):
        return [{"role": "system", "content": system_prompt or ""}] + self._messages


LEAD_PROMPT_SYSTEM_PROMPT = """You are the Voxniac Lead Prompt Generator. You are given the CURRENT sales
agent's identity/persona/guardrails (as already configured for this
business) and one specific lead's profile. Your job is to personalize a
call script for THIS lead only, while staying 100% consistent with the
agent's existing persona, tone, and guardrails — never invent a different
company, product, or price than what's given.

Output ONLY a JSON object with exactly these two keys, nothing else (no
markdown fences, no prose before or after):

{
  "first_message": "<2-3 natural spoken sentences the agent says when the lead picks up: reference the lead's company/industry and a specific pain point, create curiosity, never a hard sales pitch>",
  "system_prompt": "<the full system prompt this agent should use for THIS call: keep every guardrail and fact from the base persona below, but weave in the lead's name, company, industry, and pain points so objection handling and qualification feel personalized to them specifically>"
}

Never invent facts, prices, or claims beyond what the base persona already
states. This is a live phone call script: no markdown, no lists, no emojis
inside the string values themselves.
"""


def _parse_lead_prompt_json(raw_text: str) -> "dict | None":
    text = (raw_text or "").strip()
    text = _JSON_FENCE_RE.sub("", text).strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return None
    first_message = parsed.get("first_message")
    system_prompt = parsed.get("system_prompt")
    if not isinstance(first_message, str) or not first_message.strip():
        return None
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        return None
    return {"first_message": first_message.strip(), "system_prompt": system_prompt.strip()}


def generate_lead_prompt(lead: dict, base_profile: dict) -> dict:
    """Calls Fireworks (gpt-oss-120b) to draft a personalized
    {"first_message","system_prompt"} for this lead, grounded in the
    business's current agent_profile.json persona (base_profile — same dict
    vz_config.AGENT_PROFILE holds). Fail loud: raises LeadsError on any LLM
    or parse failure — server.py turns that into an HTTP 502."""
    from vz_config import build_system_prompt  # local import: avoids a
    # module-load-order cycle (vz_config already imports nothing from this
    # module, so this is purely defensive/explicit, not required today).

    base_persona = build_system_prompt(base_profile) or base_profile.get("system_prompt", "")
    pain_points = lead.get("painPoints") or []
    pain_points_str = ", ".join(pain_points) if pain_points else "unspecified operational challenges"

    user_message = (
        f"BASE AGENT PERSONA (keep all facts/guardrails from this):\n{base_persona}\n\n"
        f"LEAD PROFILE:\n"
        f"- Contact: {lead.get('contactName', '')}\n"
        f"- Company: {lead.get('companyName', '')}\n"
        f"- Industry: {lead.get('industry') or 'unknown'}\n"
        f"- Company size: {lead.get('companySize') or 'unknown'}\n"
        f"- Seniority: {lead.get('seniority') or 'unknown'}\n"
        f"- Known pain points: {pain_points_str}\n"
    )

    history = _SimpleHistory(user_message)
    try:
        raw_text, _ttft, _total = stream_chat(
            LEAD_LLM_MODEL_ID,
            LEAD_PROMPT_REASONING_EFFORT,
            history,
            on_token=None,
            system_prompt=LEAD_PROMPT_SYSTEM_PROMPT,
            max_tokens=LEAD_PROMPT_MAX_TOKENS,
            timeout=LEAD_PROMPT_TIMEOUT_S,
        )
    except Exception as exc:
        raise LeadsError(f"Lead prompt generation LLM call failed: {exc}") from exc

    parsed = _parse_lead_prompt_json(raw_text)
    if parsed is None:
        logger.error("leads: prompt generation returned unparseable JSON: %r", raw_text[:300])
        raise LeadsError("Lead prompt generation returned an unparseable response.")
    return parsed


# ---------------------------------------------------------------------------
# LLM: post-call classification (port of neura-sales's classify-lead-
# sentiment flow — src/ai/flows/classify-lead-sentiment.ts — the piece
# neura-sales itself defined but never wired to a real call. Cabled here to
# our own Twilio calls: server.py runs this when a /ws/twilio session with a
# lead_id ends.)
# ---------------------------------------------------------------------------
CLASSIFY_SYSTEM_PROMPT = """You are an AI sales manager analyzing a call transcript from your voice
agent. Classify the lead based on this transcript.

The voice agent may have emitted one or more action tags inside the transcript to signal state changes:
- [CONCLUDE]: means the call ended successfully (the lead is interested, wants a meeting, or gave info). This is a strong indicator of HOT.
- [ESCALATE]: means the lead requested human handoff, had a complex technical question, or needs senior follow-up. This is a strong indicator of HOT or WARM.
- [HANGUP]: means the line was hung up. If the lead hung up angrily or refused to talk, it is COLD. If they talked but then hung up, it could be COLD or WARM.

Classification criteria:
- HOT: the lead provided their email/contact info AND confirmed they have
  the pain point or need a solution (or the agent emitted [CONCLUDE] or [ESCALATE] indicating high interest/handoff request). A human closer should call ASAP.
- WARM: the lead engaged, maybe gave partial info, but was hesitant, asked
  to be sent info, or the pain point wasn't fully confirmed (or the agent emitted [ESCALATE] indicating handoff/more info needed). Needs follow-up.
- COLD: the lead hung up quickly, was angry, refused to give info, or
  explicitly said they have no interest (or the agent emitted [HANGUP] early due to complete disinterest).

Output ONLY a JSON object with exactly these two keys, nothing else (no
markdown fences, no prose before or after):

{
  "status": "<COLD, WARM, or HOT — exactly one of these three words>",
  "reasoning": "<one or two short sentences explaining the classification, grounded strictly in what's in the transcript below>"
}
"""


def _parse_classification_json(raw_text: str) -> "dict | None":
    text = (raw_text or "").strip()
    text = _JSON_FENCE_RE.sub("", text).strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return None
    status = parsed.get("status")
    reasoning = parsed.get("reasoning")
    if not isinstance(status, str) or status.strip().upper() not in STATUSES:
        return None
    if not isinstance(reasoning, str):
        reasoning = ""
    return {"status": status.strip().upper(), "reasoning": reasoning.strip()}


def classify_lead_call(lead: dict, transcript_text: str) -> dict:
    """Calls Fireworks (gpt-oss-120b) to classify a finished call's transcript
    as COLD/WARM/HOT with a short reasoning string. Fail loud: raises
    LeadsError on any LLM or parse failure — caller (server.py's background
    classification task) logs it and leaves the lead's status untouched
    rather than guessing."""
    user_message = (
        f"Lead: {lead.get('contactName', 'Unknown')} at {lead.get('companyName', 'Unknown')}\n\n"
        f"Call transcript:\n{transcript_text or '(empty transcript — call ended with no logged turns)'}"
    )
    history = _SimpleHistory(user_message)
    try:
        raw_text, _ttft, _total = stream_chat(
            LEAD_LLM_MODEL_ID,
            CLASSIFY_REASONING_EFFORT,
            history,
            on_token=None,
            system_prompt=CLASSIFY_SYSTEM_PROMPT,
            max_tokens=CLASSIFY_MAX_TOKENS,
            timeout=CLASSIFY_TIMEOUT_S,
        )
    except Exception as exc:
        raise LeadsError(f"Lead classification LLM call failed: {exc}") from exc

    parsed = _parse_classification_json(raw_text)
    if parsed is None:
        logger.error("leads: classification returned unparseable JSON: %r", raw_text[:300])
        raise LeadsError("Lead classification returned an unparseable response.")
    return parsed
