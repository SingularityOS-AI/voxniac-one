"""
interviewer.py — Voxniac ONE Phase 3, Layer 1: the onboarding "promptizer".

A conversational state machine that turns a founder's raw knowledge about
their own business into a structured `agent_profile.json` (prompt_blocks +
agent_opening + truth_base — Phase 3.5 P3's ElevenLabs-derived structure,
see vz_config.build_system_prompt) — the file the live voice cascade
(cascade.py / vz_llm.py) already reads for every call. This module never
touches audio; it is a pure text chat, driven from server.py's
`WS /ws/interview` handler (and, since Phase 3.5 P2, also fed transcribed
voice notes via `POST /interview/audio`).

State machine (INTERVIEWING -> REVIEWING_PLAN -> APPROVED, with `back` from
REVIEWING_PLAN to INTERVIEWING):

  INTERVIEWING    The interviewer LLM (gpt-oss-120b by default, reasoning_
                  effort "high", a distinct persona from the phone-call
                  agent; the model id is configurable — see Phase 3.5 P2's
                  vz_config.get_interviewer_model_id()) asks one question at
                  a time, free-form answers accepted. When it decides it has
                  covered every required topic, it appends a machine-only
                  marker to its own reply; the backend detects that marker
                  and automatically triggers plan drafting.
  REVIEWING_PLAN  A second LLM call (different system prompt, same model)
                  turns the full interview transcript into a single JSON
                  object: agent_opening, prompt_blocks (ElevenLabs-derived
                  personality/environment/tone/goal/guardrails blocks — Phase
                  3.5 P3), and truth_base (the raw structured fields).
                  Presented to the founder for approval. `back` discards the
                  draft and returns to INTERVIEWING with all prior answers
                  intact.
  APPROVED        Only on an explicit `approve` command: writes
                  agent_profile.json (prompt_blocks/agent_opening/voice/
                  llm_model + truth_base) and hot-reloads it via
                  vz_config.reload_agent_profile() so the NEXT call picks it
                  up. In-flight calls are unaffected except as documented on
                  reload_agent_profile() itself.

State lives entirely on the backend (this module + onboarding_state.json,
bulletproof-loaded next to this file, same pattern as agent_profile.json) —
closing and reopening the browser resumes exactly where the interview left
off. The client only ever renders what the server reports.

Fail loud: every LLM/parse/IO failure raises InterviewerError with a clear
message; server.py turns that into a {"type":"error"} WS event and the
session keeps running — a failed turn never kills the interview.
"""

import json
import logging
import re
import threading
from pathlib import Path

from vz_config import (
    AGENT_PROFILE_PATH,
    get_interviewer_model_id,
    load_agent_profile,
    reload_agent_profile,
)
from vz_llm import stream_chat

logger = logging.getLogger("voxniac_one.interviewer")

THIS_DIR = Path(__file__).resolve().parent
ONBOARDING_STATE_PATH = THIS_DIR / "onboarding_state.json"

# Reasoning effort is EXACT per PLAN_FASE3.md — never added to
# vz_llm.MODELOS_LLM (that table is the voice-call selector only). The model
# id itself is Phase 3.5 P2's configurable choice (120B quality / 20B fast,
# see vz_config.get_interviewer_model_id/set_interviewer_model_id) — resolved
# live at each call site below, never cached in a module constant, so a
# change from the Agent Setup UI select takes effect on the very next turn.
INTERVIEWER_REASONING_EFFORT = "high"
INTERVIEWER_MAX_TOKENS = 700
INTERVIEWER_TIMEOUT_S = 60  # high reasoning_effort on a 120B model can be slow to TTFT

# The plan-drafting call (REVIEWING_PLAN transition) needs a much larger budget
# than a normal interview turn: empirically verified (2026-07-09 live Fireworks
# test), `gpt-oss-120b` at `reasoning_effort:"high"` spends several hundred
# tokens on its hidden reasoning channel BEFORE emitting any visible `content`
# — at max_tokens=700 (the interview-turn budget) the budget is exhausted
# entirely during reasoning and the call returns an EMPTY response, so
# _parse_plan_json() always fails. PLAN_FASE3.md's "max_tokens: 700" directive
# is honored as-is for every INTERVIEWING turn (INTERVIEWER_MAX_TOKENS above);
# this larger, separate budget is scoped ONLY to the one-shot JSON-drafting
# call, whose entire purpose (per the same spec line) is exactly "necesita
# redactar el plan completo" — so this is a minimal, targeted fix to make that
# stated intent actually work, not a departure from it.
PLAN_DRAFT_MAX_TOKENS = 3000

READY_MARKER = "<<READY_FOR_PLAN>>"

STATES = ("INTERVIEWING", "REVIEWING_PLAN", "APPROVED")

DEFAULT_STATE = {"state": "INTERVIEWING", "messages": [], "draft": None}

KICKOFF_MESSAGE = (
    "Begin the interview now. Introduce yourself in one short sentence and ask your "
    "first question. Do not wait for the founder to speak first."
)

PLAN_DRAFT_REQUEST_MESSAGE = (
    "Draft the final onboarding plan now, based on everything discussed above. "
    "Output ONLY the JSON object as instructed — no prose, no markdown fences."
)

# ---------------------------------------------------------------------------
# Interviewer persona (INTERVIEWING # ---------------------------------------------------------------------------
INTERVIEWER_SYSTEM_PROMPT = """# Personality
 
You are the Voxniac Onboarding Interviewer, a sharp and friendly business
analyst. You help a founder turn their raw, informal knowledge of their own
business into the "truth base" that will power their AI appointment-setter's
live sales calls. You are curious and efficient, never robotic.

# Goal

Guide the founder through ONE question at a time (never a wall of questions)
until you have enough to draft a complete, sellable phone-call persona.
Cover, in whatever order feels natural to the conversation:

1. Services and prices — get real numbers, not "it depends".
2. The dream outcome the business actually sells (the result, not the
   deliverable).
3. The ideal customer profile (ICP) and their most acute, specific pain
   points.
4. How the founder wants the phone call to open (tone, angle, any must-say
   line).
5. At least 3 objections real prospects raise, and how the founder wants each
   one handled.
6. The escalation rule: what a prospect can say or ask that means "stop
   selling, hand this to a human" (the founder).

Once all six topics are covered, tell the founder in one short sentence that
you have what you need and ask if they're ready for you to draft the plan.
Do NOT draft, summarize, table, or preview the plan yourself in this chat —
a separate process drafts it. This step is important.

# Reasoning / Thinking Stream

Before you write your visible reply (or the <<READY_FOR_PLAN>> marker), you MUST always output your step-by-step analytical reasoning, thoughts, and strategy inside a `<think>...</think>` block. For example:
<think>
- Topic covered: Services and pricing.
- Current status: Founder mentioned legal translation at $50/page.
- Next step: Ask about dream outcome or ICP.
- Strategy: Formulate a conversational question asking about who their ideal clients are and what their biggest delay is.
</think>
[Your conversational question here...]

You must ALWAYS include the `<think>...</think>` block. It is a critical instruction.

# Guardrails

Ask one question at a time. Short, conversational, never a numbered list read
aloud. Accept free-form answers — extract the substance yourself, never force
the founder into a rigid format. If an answer is vague, ask ONE sharp
follow-up before moving on — one follow-up max per topic, never interrogate.
Never invent facts, prices, or claims the founder didn't give you. This step
is important.

CRITICAL HANDOFF RULE: the moment the founder gives ANY confirmation that
they're ready to see the plan (e.g. "yes", "go ahead", "looks good", "sounds
right", "let's do it", or answering yes to your own "ready for me to draft
the plan?" question) — your ENTIRE reply for that turn must be nothing but
the exact literal text <<READY_FOR_PLAN>> and absolutely nothing else: no
greeting, no closing line, no summary, no plan draft, no markdown. This step
is important. Never write <<READY_FOR_PLAN>> anywhere except as that
entire, standalone reply, and never before the founder has confirmed
readiness. Drafting the plan yourself in chat instead of emitting this exact
token is a failure — the founder will never see a plan you write here.

# Tone

Warm, direct, and efficient — like a good consultant, not a form. This is a
text chat, not a phone call: you may write more than one sentence per turn,
but keep every message tight and easy to scan.
"""

# ---------------------------------------------------------------------------
# Plan drafter persona (REVIEWING_PLAN transition — single structured turn)
# ---------------------------------------------------------------------------
PLAN_DRAFTER_SYSTEM_PROMPT = """You are the Voxniac Plan Drafter. You just finished interviewing a founder
about their business (see the conversation above). Your ONLY job now is to
turn that conversation into a single JSON object — nothing else in your
response: no markdown fences, no prose before or after, just the JSON.

Output EXACTLY this shape:

{
  "agent_opening": "<max 2 spoken sentences: the first thing the AI agent says when a prospect picks up the phone>",
  "prompt_blocks": {
    "personality": "<who the phone-call agent is: name, role, character — one short paragraph>",
    "environment": "<state clearly this is a live outbound phone call: audio can cut out, no visual channel, the prospect may sound distracted>",
    "tone": "<1-2 short spoken sentences per reply, under 30 words, natural, never re-introduce yourself (opening already played)>",
    "goal": "<numbered steps: qualify -> handle objections -> close for an appointment (booking a meeting or getting an email); appointment setter only, never close a sale/negotiate price/take payment. Teach the agent to use smart action tags [HANGUP], [ESCALATE], and [CONCLUDE] programmatically in its goals. For instance, ending a call after a successful booking must conclude with [CONCLUDE], escalation must end with [ESCALATE], and a direct refusal must end with [HANGUP]>",
    "guardrails": "<what the agent must never say or do; only state facts from truth_base below, never invent prices/claims; never transfer the call — use the escalation rule instead; repeat the two most critical rules from goal here too. Explicitly warn the agent to append the [HANGUP], [ESCALATE], or [CONCLUDE] tags at the absolute end of the sentence when terminating or escalating a conversation. It is a MUST rule.>"
  },
  "truth_base": {
    "business_name": "<string>",
    "services_and_prices": "<string, concrete numbers>",
    "dream_outcome": "<string>",
    "icp": "<string>",
    "icp_pain_points": "<string>",
    "opening_line_preferences": "<string>",
    "objections": [
      {"objection": "<string>", "response": "<string>"}
    ],
    "escalation_rule": "<string>"
  }
}

`truth_base.objections` must have at least 3 entries, grounded in what the
founder actually said.

Rules for `prompt_blocks` (ElevenLabs-derived prompting-guide structure —
5 of its 6 blocks: personality/environment/tone/goal/guardrails, each a
plain-text string with NO markdown inside it; keep the whole set well under
~1200 tokens combined, be concise):

- `personality`: who the agent is, tone, one short paragraph.
- `environment`: state plainly that this is a live outbound phone call —
  audio can cut out, there is no visual channel, the prospect may sound
  distracted or busy.
- `tone`: 1-2 short spoken sentences per reply, under 30 words, natural,
  never re-introduce yourself (the opening line already played when the
  call connects).
- `goal`: numbered steps — qualify the prospect, handle objections, close
  for an appointment (booking a meeting or getting an email). NEVER close a
  sale, negotiate price, or take payment — this agent is an appointment
  setter, not a closer. Explicitly state the action tags rules: ending a successful call must conclude with [CONCLUDE] in its text, escalation with [ESCALATE], and a quick hang up or refusal with [HANGUP]. This step is important.
- `guardrails`: never re-introduce yourself; keep replies to 1-2 short
  spoken sentences under 30 words; only state facts present in truth_base
  above — never invent prices or claims; never transfer the call — use the
  escalation rule instead. Always make sure the agent appends [HANGUP], [ESCALATE], or [CONCLUDE] at the very end of its final dialogue turn. This step is important. Repeat the two most
  critical rules here again even though they're also in `goal`:
  appointment-setter only (never close/negotiate/take payment), and never
  invent facts.

Ground every fact in `prompt_blocks` and `truth_base` strictly in what the
founder actually said in the conversation above. Do not invent specifics.

Remember: output ONLY the JSON object, nothing else — no ```json fences, no
leading or trailing prose.
"""

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# Phase 3.5 P3: the 5 canonical prompt_blocks keys the plan drafter must fill.
_REQUIRED_PROMPT_BLOCKS = ("personality", "environment", "tone", "goal", "guardrails")


class InterviewerError(Exception):
    """Raised for interviewer-flow failures that must be surfaced to the
    client as a fail-loud {"type":"error"} WS event without killing the
    socket — mirrors cascade.py's error philosophy for the voice path."""


class _HistoryView:
    """Minimal duck-type adapter so vz_llm.stream_chat can consume a plain
    message list. Deliberately NOT vz_llm.ConversationHistory: that class
    caps history at MAX_HISTORY_MESSAGES=12, a budget tuned for live voice
    turns — an onboarding interview legitimately needs the full transcript
    (it's summarized into the truth base at REVIEWING_PLAN time)."""

    def __init__(self, messages):
        self._messages = list(messages)

    def as_messages_with_system(self, system_prompt=None):
        return [{"role": "system", "content": system_prompt or ""}] + self._messages


# ---------------------------------------------------------------------------
# Persistence (bulletproof, mirrors vz_config.load_agent_profile's pattern)
# ---------------------------------------------------------------------------
def load_onboarding_state(path: Path = None) -> dict:
    path = path or ONBOARDING_STATE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.info("onboarding_state.json could not be loaded (%s); starting a fresh interview.", exc)
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    state = dict(DEFAULT_STATE)
    if raw.get("state") in STATES:
        state["state"] = raw["state"]
    if isinstance(raw.get("messages"), list):
        state["messages"] = raw["messages"]
    if isinstance(raw.get("draft"), dict):
        state["draft"] = raw["draft"]
    return state


def save_onboarding_state(state: dict, path: Path = None):
    path = path or ONBOARDING_STATE_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error("onboarding_state.json could not be saved: %s", exc)


def _try_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _parse_plan_json(raw_text: str):
    """Best-effort JSON extraction from the plan-drafter's response. Strips
    markdown code fences if present, then falls back to grabbing the
    outermost {...} block if the model added stray prose. Returns None
    (never raises) if no minimally-shaped plan object can be found.

    Phase 3.5 P3: validates the new prompt_blocks-based shape (agent_opening
    + prompt_blocks{personality,environment,tone,goal,guardrails} +
    truth_base) instead of the old flat system_prompt string."""
    text = (raw_text or "").strip()
    text = _JSON_FENCE_RE.sub("", text).strip()

    parsed = _try_json(text)
    if parsed is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            parsed = _try_json(text[start:end + 1])

    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("agent_opening"), str) or not parsed["agent_opening"].strip():
        return None
    blocks = parsed.get("prompt_blocks")
    if not isinstance(blocks, dict):
        return None
    for key in _REQUIRED_PROMPT_BLOCKS:
        if not isinstance(blocks.get(key), str) or not blocks[key].strip():
            return None
    if not isinstance(parsed.get("truth_base"), dict):
        return None
    return parsed


# ---------------------------------------------------------------------------
# InterviewSession — the state machine
# ---------------------------------------------------------------------------
class InterviewSession:
    """One onboarding interview. Synchronous/blocking (like vz_llm.stream_chat
    itself) — server.py's WS handler runs these methods in a worker thread and
    forwards on_token calls back to the event loop."""

    def __init__(self):
        self.state = load_onboarding_state()

    def _save(self):
        save_onboarding_state(self.state)

    def is_fresh(self) -> bool:
        return self.state["state"] == "INTERVIEWING" and not self.state["messages"]

    def snapshot(self) -> dict:
        draft = self.state.get("draft") or {}
        return {
            "type": "state",
            "state": self.state["state"],
            "fields": draft.get("truth_base", {}),
            "messages": self.state["messages"],
            "draft": draft if self.state["state"] in ("REVIEWING_PLAN", "APPROVED") else None,
        }

    # ------------------------------------------------------------------
    # INTERVIEWING turns
    # ------------------------------------------------------------------
    def opening_turn(self, on_token) -> str:
        """The interviewer speaks first when a fresh session connects."""
        if self.state["state"] != "INTERVIEWING" or self.state["messages"]:
            raise InterviewerError("opening_turn is only valid for a fresh INTERVIEWING session.")
        try:
            visible_text, ready = self._stream_interviewer_reply(
                [{"role": "user", "content": KICKOFF_MESSAGE}], on_token,
            )
        except Exception as exc:
            raise InterviewerError(f"Interviewer LLM call failed: {exc}") from exc

        self.state["messages"].append({"role": "assistant", "content": visible_text})
        self._save()
        if ready:
            self._draft_plan()
        return visible_text

    def user_turn(self, text: str, on_token) -> str:
        if self.state["state"] != "INTERVIEWING":
            raise InterviewerError("Send 'back' to adjust the plan before chatting further.")
        text = (text or "").strip()
        if not text:
            raise InterviewerError("Empty message.")

        self.state["messages"].append({"role": "user", "content": text})
        self._save()

        try:
            visible_text, ready = self._stream_interviewer_reply(self.state["messages"], on_token)
        except Exception as exc:
            # Drop the unanswered user turn so a retry doesn't duplicate it forever
            # (same fail-loud-without-corrupting-history pattern as cascade.py).
            if self.state["messages"] and self.state["messages"][-1]["role"] == "user":
                self.state["messages"].pop()
            self._save()
            raise InterviewerError(f"Interviewer LLM call failed: {exc}") from exc

        self.state["messages"].append({"role": "assistant", "content": visible_text})
        self._save()
        if ready:
            self._draft_plan()
        return visible_text

    def _stream_interviewer_reply(self, messages_for_call, on_token):
        history = _HistoryView(messages_for_call)
        full_text, _ttft, _total = stream_chat(
            get_interviewer_model_id(),
            INTERVIEWER_REASONING_EFFORT,
            history,
            on_token=on_token,
            stop_event=threading.Event(),
            system_prompt=INTERVIEWER_SYSTEM_PROMPT,
            max_tokens=INTERVIEWER_MAX_TOKENS,
            timeout=INTERVIEWER_TIMEOUT_S,
        )
        ready = READY_MARKER in full_text
        visible_text = full_text.replace(READY_MARKER, "").strip()
        # Strip <think>...</think> blocks from visible_text to avoid saving thoughts to chat message history
        visible_text = re.sub(r"<think>.*?</think>", "", visible_text, flags=re.DOTALL).strip()
        if ready and not visible_text:
            # The marker is meant to be the model's ENTIRE reply once the
            # founder confirms readiness (see the CRITICAL HANDOFF RULE in
            # INTERVIEWER_SYSTEM_PROMPT) — show a friendly line instead of a
            # blank chat bubble while the plan drafts in the background.
            visible_text = "Great — let me put your plan together now."
        return visible_text, ready

    # ------------------------------------------------------------------
    # REVIEWING_PLAN transition (automatic, triggered by the marker)
    # ------------------------------------------------------------------
    def _draft_plan(self) -> bool:
        """Runs the JSON-drafting call. Returns True on success (state becomes
        REVIEWING_PLAN). On any failure, logs it and leaves the state
        unchanged (INTERVIEWING) — the founder just keeps chatting and the
        interviewer will naturally offer to wrap up again. This is
        deliberately non-raising: the interviewer's own reply for this turn
        has already been streamed to the client as interviewer_done, and a
        drafting hiccup shouldn't retroactively turn a good turn into an
        error."""
        drafting_messages = self.state["messages"] + [
            {"role": "user", "content": PLAN_DRAFT_REQUEST_MESSAGE}
        ]
        history = _HistoryView(drafting_messages)
        try:
            raw_text, _ttft, _total = stream_chat(
                get_interviewer_model_id(),
                INTERVIEWER_REASONING_EFFORT,
                history,
                on_token=None,
                stop_event=threading.Event(),
                system_prompt=PLAN_DRAFTER_SYSTEM_PROMPT,
                max_tokens=PLAN_DRAFT_MAX_TOKENS,
                timeout=INTERVIEWER_TIMEOUT_S,
            )
        except Exception as exc:
            logger.error("interviewer: plan drafting LLM call failed: %s", exc)
            return False

        draft = _parse_plan_json(raw_text)
        if draft is None:
            logger.error("interviewer: plan drafting returned unparseable JSON: %r", raw_text[:300])
            return False

        self.state["draft"] = draft
        self.state["state"] = "REVIEWING_PLAN"
        self._save()
        return True

    # ------------------------------------------------------------------
    # REVIEWING_PLAN commands
    # ------------------------------------------------------------------
    def approve(self) -> dict:
        """Writes agent_profile.json and hot-reloads it. Only valid from
        REVIEWING_PLAN. voice/llm_model are preserved from the CURRENT file
        unless the draft explicitly set them (it normally doesn't).

        Phase 3.5 P3: writes the new prompt_blocks-based structure. Also
        tolerates a legacy draft shape (a flat "system_prompt" instead of
        "prompt_blocks") without crashing — should never happen with the
        current PLAN_DRAFTER_SYSTEM_PROMPT/_parse_plan_json, but a draft
        left over in onboarding_state.json from before this change must not
        make approve() blow up."""
        if self.state["state"] != "REVIEWING_PLAN":
            raise InterviewerError("Nothing to approve: interview is not in REVIEWING_PLAN state.")
        draft = self.state.get("draft")
        if not draft:
            raise InterviewerError("No draft plan found to approve.")

        current = load_agent_profile()
        profile = {
            "agent_opening": draft.get("agent_opening") or current["agent_opening"],
            "voice": draft.get("voice") or current["voice"],
            "llm_model": draft.get("llm_model") or current["llm_model"],
            "truth_base": draft.get("truth_base", {}),
        }
        if isinstance(draft.get("prompt_blocks"), dict) and draft["prompt_blocks"]:
            profile["prompt_blocks"] = draft["prompt_blocks"]
        elif isinstance(draft.get("system_prompt"), str) and draft["system_prompt"].strip():
            # Legacy fallback (pre-Phase-3.5 draft shape) — never crash on it.
            profile["system_prompt"] = draft["system_prompt"]
        else:
            profile["system_prompt"] = current.get("system_prompt", "")

        try:
            with open(AGENT_PROFILE_PATH, "w", encoding="utf-8") as f:
                json.dump(profile, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            raise InterviewerError(f"Could not write agent_profile.json: {exc}") from exc

        reload_agent_profile()

        self.state["state"] = "APPROVED"
        self._save()
        return profile

    def back(self):
        """REVIEWING_PLAN -> INTERVIEWING, discarding the draft but keeping
        every answer already given (they're still in self.state["messages"])."""
        if self.state["state"] != "REVIEWING_PLAN":
            raise InterviewerError("Nothing to go back from (not in REVIEWING_PLAN).")
        self.state["state"] = "INTERVIEWING"
        self.state["draft"] = None
        self._save()

    def reset(self):
        """Wipes the interview entirely and starts over."""
        self.state = {"state": "INTERVIEWING", "messages": [], "draft": None}
        self._save()
