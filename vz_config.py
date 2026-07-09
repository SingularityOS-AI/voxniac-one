"""
vz_config.py — Credentials (.env) and configuration loading for Voxniac ONE.

Bulletproof-config pattern:
- Missing or broken JSON -> defaults, never an exception.
- Every value is clamped to a sane range; invalid type or out-of-range value ->
  clamp/default.
- Paths (.env, agent_profile.json) are resolved relative to THIS file, not the cwd.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Path resolution (relative to this file, not the cwd)
# ---------------------------------------------------------------------------
THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent  # C:\...\voxniac

ENV_PATH = REPO_ROOT / ".env"
GOOGLE_CREDS_PATH = REPO_ROOT / "singularityos-neural-captions-188ff3cee843.json"
CONFIG_JSON_PATH = THIS_DIR / "config.json"
AGENT_PROFILE_PATH = THIS_DIR / "agent_profile.json"

load_dotenv(dotenv_path=ENV_PATH)

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FIREWORKS_API_KEY = os.getenv("FIREWORKS_API_KEY") or os.getenv("GPT20B_API_KEY")
KOKORO_API_KEY = os.getenv("KOKORO_API_KEY")  # DeepInfra key
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# Twilio outbound-call credentials (Phase 2: real outbound calling via
# call_launcher.py). NEVER print or log these values.
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

# Google: only set the env var if it isn't already defined.
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    if GOOGLE_CREDS_PATH.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(GOOGLE_CREDS_PATH)

GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "latest_long")

_GOOGLE_CREDS_AVAILABLE = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")) and Path(
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
).exists()

try:
    import google.cloud.speech  # noqa: F401

    _GOOGLE_SDK_AVAILABLE = True
except ImportError:
    _GOOGLE_SDK_AVAILABLE = False


def engine_availability():
    """
    Returns a dict {engine_name: (available: bool, reason_if_not: str)} covering
    ASR, LLM (Fireworks backs all 4 LLM models with the same key), and TTS engines.
    """
    return {
        "groq": (bool(GROQ_API_KEY), "missing GROQ_API_KEY"),
        "deepgram": (bool(DEEPGRAM_API_KEY), "missing DEEPGRAM_API_KEY"),
        "google": (
            _GOOGLE_SDK_AVAILABLE and _GOOGLE_CREDS_AVAILABLE,
            "google-cloud-speech not installed" if not _GOOGLE_SDK_AVAILABLE else "missing GOOGLE_APPLICATION_CREDENTIALS",
        ),
        "fireworks": (bool(FIREWORKS_API_KEY), "missing FIREWORKS_API_KEY"),
        "kokoro": (bool(KOKORO_API_KEY), "missing KOKORO_API_KEY"),
        "aura2": (bool(DEEPGRAM_API_KEY), "missing DEEPGRAM_API_KEY"),
        "groq_orpheus": (bool(GROQ_API_KEY), "missing GROQ_API_KEY"),
    }


# ---------------------------------------------------------------------------
# Phase 3.5 P2: onboarding interviewer model selector (120B quality / 20B fast).
# Configurable via env var (checked live, no restart needed) or config.json's
# "interviewer.model_id" (persisted by set_interviewer_model_id(), used by the
# Agent Setup UI select). Never any other model id is accepted — bulletproof:
# an unknown/missing value always falls back to DEFAULT_INTERVIEWER_MODEL_ID.
# ---------------------------------------------------------------------------
INTERVIEWER_MODEL_CHOICES = {
    "accounts/fireworks/models/gpt-oss-120b": "GPT-OSS-120B (quality)",
    "accounts/fireworks/models/gpt-oss-20b": "GPT-OSS-20B (fast)",
}
DEFAULT_INTERVIEWER_MODEL_ID = "accounts/fireworks/models/gpt-oss-120b"

# ---------------------------------------------------------------------------
# Bulletproof config: config.json ("vad" + "interviewer" sections)
# ---------------------------------------------------------------------------
# VAD config is kept as fallback/legacy tuning surface (batch ASR path, local
# silence heuristics) even though the live pipeline relies on Deepgram's own
# endpointing/vad_events for turn-taking.
DEFAULT_CONFIG = {
    "vad": {
        "silence_ms": 1500,
        "onset_ms": 180,
        "abs_floor": 0.012,
        "noise_mult": 3.2,
        "min_utter_ms": 300,
        "max_utterance_ms": 30000,
    },
    "interviewer": {
        "model_id": DEFAULT_INTERVIEWER_MODEL_ID,
    },
}

# range: (min, max) — applied to every numeric value
_RANGES = {
    "silence_ms": (300, 5000),
    "onset_ms": (60, 1000),
    "abs_floor": (0.001, 0.2),
    "noise_mult": (1.0, 10.0),
    "min_utter_ms": (100, 2000),
    "max_utterance_ms": (5000, 60000),
}


def _clamp_value(key, value):
    """Clamps a numeric value to its safe range. Invalid type -> default."""
    default = DEFAULT_CONFIG["vad"][key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return default
    lo, hi = _RANGES[key]
    try:
        clamped = max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default
    # Keep int type when the default is int and the field isn't a float range.
    if isinstance(default, int) and key != "abs_floor" and key != "noise_mult":
        return int(clamped)
    return clamped


def _deep_merge_vad(raw_vad):
    """Merges raw_vad (possibly partial/corrupt) with the defaults, clamping each field."""
    merged = dict(DEFAULT_CONFIG["vad"])
    if isinstance(raw_vad, dict):
        for key in DEFAULT_CONFIG["vad"]:
            if key in raw_vad:
                merged[key] = _clamp_value(key, raw_vad[key])
    return merged


def _merge_interviewer(raw_interviewer):
    """Merges raw_interviewer (possibly partial/corrupt) with the default,
    accepting only a model_id that's one of INTERVIEWER_MODEL_CHOICES."""
    model_id = DEFAULT_INTERVIEWER_MODEL_ID
    if isinstance(raw_interviewer, dict):
        candidate = raw_interviewer.get("model_id")
        if isinstance(candidate, str) and candidate in INTERVIEWER_MODEL_CHOICES:
            model_id = candidate
    return {"model_id": model_id}


def load_config(path: Path = None) -> dict:
    """
    Loads config.json in a bulletproof way:
    - Missing file or invalid JSON -> full defaults.
    - Missing keys -> filled in with defaults.
    - Out-of-range or wrong-typed values -> clamp/default, never an exception.
    """
    path = path or CONFIG_JSON_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        raw = {}

    if not isinstance(raw, dict):
        raw = {}

    vad = _deep_merge_vad(raw.get("vad"))
    interviewer = _merge_interviewer(raw.get("interviewer"))
    return {"vad": vad, "interviewer": interviewer}


CONFIG = load_config()
VAD_CONFIG = CONFIG["vad"]
INTERVIEWER_CONFIG = CONFIG["interviewer"]


def get_interviewer_model_id() -> str:
    """Phase 3.5 P2: resolves the onboarding interviewer's model id, checked
    live (never cached) so a config.json write via set_interviewer_model_id()
    or an env var change takes effect on the very next interview turn.
    Precedence: INTERVIEWER_MODEL_ID env var (if it's a known choice) >
    config.json's persisted interviewer.model_id > DEFAULT_INTERVIEWER_MODEL_ID.
    """
    env_value = os.environ.get("INTERVIEWER_MODEL_ID")
    if env_value in INTERVIEWER_MODEL_CHOICES:
        return env_value
    return INTERVIEWER_CONFIG.get("model_id", DEFAULT_INTERVIEWER_MODEL_ID)


def set_interviewer_model_id(model_id: str, path: Path = None) -> bool:
    """Phase 3.5 P2: validates model_id against INTERVIEWER_MODEL_CHOICES,
    persists it to config.json (preserving the rest of the file's content,
    e.g. "vad"), and updates INTERVIEWER_CONFIG in place so
    get_interviewer_model_id() picks it up immediately, in this process,
    with no restart. Returns False (bulletproof no-op, never raises) for an
    unknown model_id or if the file can't be written.
    """
    if model_id not in INTERVIEWER_MODEL_CHOICES:
        return False

    path = path or CONFIG_JSON_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        raw = {}

    raw["interviewer"] = {"model_id": model_id}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"[!] Could not persist interviewer model choice to config.json: {exc}")
        return False

    INTERVIEWER_CONFIG["model_id"] = model_id
    return True


# ---------------------------------------------------------------------------
# Phase 3.5 P3: structured prompt_blocks (ElevenLabs-derived 5-block system
# prompt) used as the embedded fallback's blocks. See build_system_prompt()
# below for how these compose into the final system prompt string, and
# load_agent_profile() for how agent_profile.json's own "prompt_blocks" key
# overrides these when present.
# ---------------------------------------------------------------------------
_DEFAULT_PROMPT_BLOCKS = {
    "personality": (
        "You are Sharon, Director of Operations at SingularityOS AI LLC — a "
        "Wyoming-based firm delivering HIPAA-compliant medical interpretation "
        "and USCIS-certified legal translation. Your tone is executive, "
        "authoritative, and clinical. You are a solution provider, not a "
        "telemarketer."
    ),
    "environment": (
        "This is a live outbound phone call. Audio can cut out or sound "
        "noisy; the person you're calling may sound confused, distracted, or "
        "busy. There is no visual channel — everything must land through "
        "voice alone."
    ),
    "tone": (
        "1-2 short spoken sentences per reply, under 30 words total. One "
        "idea, then a question. Natural and conversational, never a script "
        "read aloud. Never re-introduce yourself — your opening line already "
        "played when the call connected."
    ),
    "goal": (
        "You are an APPOINTMENT SETTER, not a closer. (1) Qualify — identify "
        "if they're legal or medical and name their specific pain. (2) "
        "Handle objections with silver bullets: legitimacy (Wyoming "
        "corporation, 100% court acceptance), compliance (HIPAA, Zero Data "
        "Retention), speed (24-48h). (3) Close for the appointment: offer to "
        "send the founder's Calendly and a technical breakdown, then ask for "
        "the best corporate email. Once you have the email, confirm and end "
        "warmly. You never negotiate final price, sign contracts, or take "
        "payment — a human closes the booked meeting."
    ),
    "guardrails": (
        "NEVER re-introduce yourself or restart your pitch — if interrupted "
        "or asked to repeat, continue naturally in ONE short sentence. NO "
        "SMALL TALK: if asked 'how are you', reply 'I'm well. Let's get to "
        "business.' NEVER transfer the call — if the prospect demands the "
        "CEO or challenges your legitimacy, say you'll have founder Gabriel "
        "Bustos reach out personally and ask for their best corporate email. "
        "Anchor facts only: legal translation is $29/page (24-48h delivery, "
        "USCIS-accepted on first submission, zero document retention); "
        "medical interpretation is $75/hour (HIPAA and IMIA certified, "
        "real-time). Never invent prices or claims outside these. This is a "
        "live phone call: no markdown, no lists, no emojis."
    ),
}


# ---------------------------------------------------------------------------
# Bulletproof config: agent_profile.json (persona, prompts, voice, default model)
# ---------------------------------------------------------------------------
# Embedded safe defaults — used field by field whenever agent_profile.json is
# missing, corrupt, or a key is absent/invalid. This must never raise: the
# agent has to be able to start a call even with a broken profile file.
# "system_prompt" is kept as an extra-safe flat fallback (used only if
# "prompt_blocks" ever ends up missing/malformed — see build_system_prompt()).
_DEFAULT_AGENT_PROFILE = {
    "system_prompt": (
        "You are Sharon, Director of Operations at SingularityOS AI LLC — a Wyoming-based "
        "firm delivering HIPAA-compliant medical interpretation and USCIS-certified legal "
        "translation. Your tone is executive, authoritative, and clinical. You are a "
        "solution provider, not a telemarketer.\n\n"
        "GOAL: You are an APPOINTMENT SETTER, not a closer. Your only positive outcome is "
        "booking a consultation. You never negotiate final price, sign contracts, or take "
        "payment — a human closes the booked meeting.\n\n"
        "RULES:\n"
        "- Your opening line has ALREADY been spoken when the call connects. NEVER "
        "re-introduce yourself, never repeat your opening, never restart your pitch. If "
        "the person says 'Hello?', seems confused, or asks you to repeat, continue "
        "naturally from where you left off in ONE short sentence.\n"
        "- HARD LIMIT: 1-2 short spoken sentences per reply (under 30 words total). One "
        "idea, then a question. Real setters are brief.\n"
        "- NO LOOPING: if interrupted, answer directly; never restart your script.\n"
        "- NO SMALL TALK: if asked 'how are you', reply 'I'm well. Let's get to business.'\n"
        "- NEVER transfer the call. If the prospect demands the CEO or challenges your "
        "legitimacy in a way you can't resolve, say: 'I'll have our founder, Gabriel "
        "Bustos, reach out personally — what's the best corporate email?'\n"
        "- Anchor facts only: Legal translation is $29/page, delivered in 24-48h, "
        "USCIS-accepted on first submission, zero document retention. Medical "
        "interpretation is $75/hour, HIPAA and IMIA certified, real-time. Never invent "
        "prices or claims outside these.\n\n"
        "FLOW: (1) Qualify — identify if they're legal or medical and name their specific "
        "pain. (2) Handle objections with silver bullets: legitimacy (Wyoming corporation, "
        "100% court acceptance), compliance (HIPAA, Zero Data Retention), speed (24-48h). "
        "(3) Close for the appointment: 'Your operation is a fit. I'll send our founder's "
        "Calendly and a technical breakdown — what's the best corporate email?' Once you "
        "have the email, confirm and end warmly.\n\n"
        "This is a live phone call: no markdown, no lists, no emojis."
    ),
    "agent_opening": (
        "Hi, this is Sharon with SingularityOS. Quick question — are you the person who "
        "handles certified translation or medical interpretation for your firm?"
    ),
    "voice": "aura-2-thalia-en",
    "llm_model": "accounts/fireworks/models/kimi-k2p6",
    "prompt_blocks": _DEFAULT_PROMPT_BLOCKS,
}


def load_agent_profile(path: Path = None) -> dict:
    """
    Bulletproof loader for agent_profile.json. A missing file, invalid JSON, or a
    missing/invalid key never raises — each field independently falls back to the
    embedded safe default. This guarantees the agent can always start a call.

    Phase 3.5 P3: "prompt_blocks" (dict of personality/environment/tone/goal/
    guardrails strings) and "truth_base" (dict, the interviewer's raw
    structured fields) are new, optional keys layered on top of the original
    flat-string fields. Retrocompatible: an agent_profile.json with only the
    old flat "system_prompt" (no "prompt_blocks") loads exactly as before —
    build_system_prompt() falls back to that flat string.
    """
    path = path or AGENT_PROFILE_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"[!] agent_profile.json could not be loaded ({exc}); using embedded defaults.")
        raw = {}

    if not isinstance(raw, dict):
        print("[!] agent_profile.json did not contain a JSON object; using embedded defaults.")
        raw = {}

    profile = dict(_DEFAULT_AGENT_PROFILE)
    for key in _DEFAULT_AGENT_PROFILE:
        if key == "prompt_blocks":
            continue  # handled separately below (dict, not a flat string)
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            profile[key] = value

    # "prompt_blocks": only override the embedded default if the file's value
    # is itself a non-empty dict (bulletproof — a malformed value just keeps
    # the embedded _DEFAULT_PROMPT_BLOCKS).
    if isinstance(raw.get("prompt_blocks"), dict) and raw["prompt_blocks"]:
        profile["prompt_blocks"] = raw["prompt_blocks"]

    # "truth_base": no embedded default (it's purely informational metadata
    # from the interviewer) — present only when the file has a valid dict.
    if isinstance(raw.get("truth_base"), dict):
        profile["truth_base"] = raw["truth_base"]

    return profile


def build_system_prompt(profile: dict) -> str:
    """
    Phase 3.5 P3 — deterministic system-prompt builder (ElevenLabs-derived
    prompt_blocks structure). Composes profile["prompt_blocks"] (personality,
    environment, tone, goal, guardrails — in that order) plus, if present,
    profile["truth_base"] into one system-prompt string with "## SECTION"
    markdown headers.

    Retrocompatible by design: if profile has no "prompt_blocks" (or it's not
    a non-empty dict — e.g. an old-style profile with only a flat
    "system_prompt"), returns profile.get("system_prompt", "") unchanged, so
    every profile written before Phase 3.5 keeps working exactly as before.

    Never raises: unexpected types inside prompt_blocks/truth_base are
    stringified defensively rather than blowing up a live call.
    """
    blocks = profile.get("prompt_blocks")
    if not isinstance(blocks, dict) or not blocks:
        return profile.get("system_prompt", "") or ""

    order = ("personality", "environment", "tone", "goal", "guardrails")
    sections = []
    for key in order:
        value = blocks.get(key)
        if isinstance(value, str) and value.strip():
            sections.append(f"## {key.upper()}\n{value.strip()}")

    # Forward-compatible: any extra custom block beyond the 5 canonical ones
    # is still included (never silently dropped), appended after the
    # canonical order.
    for key, value in blocks.items():
        if key in order:
            continue
        if isinstance(value, str) and value.strip():
            sections.append(f"## {str(key).upper()}\n{value.strip()}")

    truth_base = profile.get("truth_base")
    if isinstance(truth_base, dict) and truth_base:
        tb_lines = []
        for key, value in truth_base.items():
            if isinstance(value, list):
                parts = []
                for item in value:
                    if isinstance(item, dict):
                        parts.append(f"{item.get('objection', '')} -> {item.get('response', '')}")
                    else:
                        parts.append(str(item))
                value_str = "; ".join(parts)
            else:
                value_str = str(value)
            tb_lines.append(f"- {key}: {value_str}")
        if tb_lines:
            sections.append("## TRUTH_BASE\n" + "\n".join(tb_lines))

    return "\n\n".join(sections).strip()


def get_effective_system_prompt(profile: "dict | None" = None) -> str:
    """
    Phase 3.5 P3 — the single accessor every caller should use to read "the
    system prompt" instead of profile.get("system_prompt", ...) directly.
    Defaults to the module-level AGENT_PROFILE (so vz_config.reload_agent_
    profile()'s in-place mutation is always picked up fresh, same hot-reload
    guarantee as before) but accepts an explicit profile dict too.

    See vz_llm.py's ConversationHistory.as_messages_with_system(), the one
    place in this codebase that actually reads the live system prompt for a
    voice call — this is a drop-in replacement for its previous
    `AGENT_PROFILE.get("system_prompt", "")` read.
    """
    profile = profile if profile is not None else AGENT_PROFILE
    return build_system_prompt(profile)


AGENT_PROFILE = load_agent_profile()


def reload_agent_profile(path: Path = None) -> dict:
    """
    Phase 3 hot-reload: re-reads agent_profile.json (bulletproof, same rules as
    load_agent_profile) and mutates the existing AGENT_PROFILE dict IN PLACE
    (clear + update) instead of rebinding the module-level name. This matters
    because every module that did `from vz_config import AGENT_PROFILE` (server.py,
    cascade.py via the profile it's handed, vz_llm.py) holds a reference to that
    same dict object — rebinding `AGENT_PROFILE = ...` here would only update
    vz_config's own local name and leave every other module's reference stale.
    Mutating in place means all of them observe the new content immediately.

    Practical effect: any NEW call (new CascadeSession) picks up the reloaded
    profile from its first read. Because the dict is shared by reference, a
    call already in flight when reload happens will also observe the new
    values on its NEXT field read (e.g. its next turn's llm_model/system_prompt) —
    except for the opening line and TTS voice, which cascade.py reads once at
    session start() and keeps for the rest of that call. There is no snapshot
    isolation beyond that; this codebase never modifies cascade.py to add one
    (out of scope for Phase 3 — see PLAN_FASE3.md section C).

    Called by interviewer.py right after APPROVED writes agent_profile.json.
    """
    fresh = load_agent_profile(path)
    AGENT_PROFILE.clear()
    AGENT_PROFILE.update(fresh)
    return AGENT_PROFILE
