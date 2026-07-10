"""
vz_logger.py — Per-turn logging for Voxniac ONE.

Appends one JSON line per conversation turn to voxniac_one_log.jsonl and,
optionally, writes the concatenated TTS audio for that turn as a WAV file under
recordings/. Never raises — any logging failure is printed as a warning and the
turn continues unaffected (non-negotiable principle: logging can't crash a call).

Phase 3.5 P1 additions: make_call_id() builds a per-call identifier (passed
into log_turn()'s entry dict by cascade.py as "call_id"/"channel") and
write_call_transcript() renders transcripts/CALL_<call_id>.md by filtering
this same JSONL log for a given call_id — called from server.py's /ws/twilio
handler when a phone call ends (including on an abnormal drop, from a
finally block), so a call's transcript is never lost.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
LOG_PATH = THIS_DIR / "voxniac_one_log.jsonl"
RECORDINGS_DIR = THIS_DIR / "recordings"
TRANSCRIPTS_DIR = THIS_DIR / "transcripts"

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_\-]+")
_NON_DIGIT_RE = re.compile(r"\D+")


def _slug(text: str) -> str:
    return _SAFE_CHARS_RE.sub("_", text or "na").strip("_") or "na"


def _ensure_recordings_dir():
    try:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[!] Could not create recordings/: {exc}")


def log_turn(entry: dict) -> dict:
    """
    Appends `entry` as one JSON line to voxniac_one_log.jsonl.

    Expected fields (not strict — whatever is passed gets logged as-is):
      timestamp, llm_model, user_text, agent_text, stt_final_s, ttft_s, ttfa_s,
      e2e_s, tts_wav_bytes (optional; persisted to disk and replaced by
      tts_wav_path in the logged entry).

    Returns the entry (with timestamp/tts_wav_path filled in) so the caller can
    still use it even if logging itself fails. NEVER raises.
    """
    entry = dict(entry)
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    wav_bytes = entry.pop("tts_wav_bytes", None)
    tts_wav_path = None

    if wav_bytes:
        try:
            _ensure_recordings_dir()
            model_slug = _slug(str(entry.get("llm_model", "na")))
            ts_slug = _slug(entry["timestamp"]) or str(int(time.time()))
            fname = f"{ts_slug}_{model_slug}.wav"
            fpath = RECORDINGS_DIR / fname
            with open(fpath, "wb") as f:
                f.write(wav_bytes)
            tts_wav_path = str(fpath)
        except OSError as exc:
            print(f"[!] Could not save TTS WAV: {exc}")

    entry["tts_wav_path"] = tts_wav_path

    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[!] Could not write log: {exc}")

    return entry


# ---------------------------------------------------------------------------
# Phase 3.5 P1: call_id + per-call transcript
# ---------------------------------------------------------------------------
def make_call_id(prefix: str = "", phone: "str | None" = None) -> str:
    """
    Builds a per-call identifier used to tag every turn logged during one
    call (log_turn's "call_id" field) and to name its transcript file.

    - Twilio calls: make_call_id(phone="+13075550100") -> "20260709_143207_0100"
      (timestamp + last 4 digits of the phone number; "0000" if no/short phone).
    - Browser calls: make_call_id(prefix="browser") -> "browser_20260709_143207"
      (no phone number to key on).

    Seconds-resolution timestamp (not just hour:minute, despite the example
    in PLAN_FASE3_5.md using HHMM) to avoid two calls in the same minute
    colliding on the same transcript filename. Never raises.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if prefix:
        return f"{prefix}_{ts}"
    digits = _NON_DIGIT_RE.sub("", phone or "")
    last4 = digits[-4:] if len(digits) >= 4 else "0000"
    return f"{ts}_{last4}"


def _mask_phone(phone: "str | None") -> str:
    """Never expose a full phone number in a shareable transcript — only the
    last 4 digits (mirrors the "no full phone numbers in filenames, last 4
    digits OK" house rule from PLAN_FASE3_5.md, applied to file content too)."""
    if not phone:
        return "unknown"
    digits = _NON_DIGIT_RE.sub("", phone)
    if len(digits) < 4:
        return "unknown"
    return f"+…{digits[-4:]}"


def _ensure_transcripts_dir():
    try:
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[!] Could not create transcripts/: {exc}")


def _read_turns_for_call(call_id: str) -> list:
    """Reads voxniac_one_log.jsonl (if any) and returns every entry whose
    call_id matches, in file order (the log is append-only, so this is
    chronological). Never raises — a missing/corrupt log yields []."""
    turns = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("call_id") == call_id:
                    turns.append(entry)
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []
    return turns


def get_call_transcript_text(call_id: str) -> str:
    """Phase 4 Etapa C: plain-text "User: ...\\nAgent: ..." transcript for a
    call, built from the same voxniac_one_log.jsonl entries write_call_
    transcript() reads — used by server.py to feed a finished lead call's
    transcript into leads.classify_lead_call() (Fireworks). Never raises
    (same bulletproof contract as the rest of this module): a missing/
    corrupt log yields an empty string, which classify_lead_call() itself
    already handles gracefully."""
    turns = _read_turns_for_call(call_id)
    lines = []
    for turn in turns:
        lines.append(f"User: {turn.get('user_text', '')}")
        lines.append(f"Agent: {turn.get('agent_text', '')}")
    return "\n".join(lines)


def write_call_transcript(
    call_id: str,
    phone: "str | None" = None,
    started_at=None,
    ended_at=None,
    llm_model: "str | None" = None,
) -> "Path | None":
    """
    Writes transcripts/CALL_<call_id>.md: phone (masked to last 4 digits),
    start/end time, LLM model, and the user/agent turn sequence with
    per-turn metrics — reconstructed from voxniac_one_log.jsonl entries
    tagged with this call_id (see cascade.py's log_turn() call).

    Intended to be called from a `finally` block when a Twilio call's
    WebSocket session ends (server.py's /ws/twilio handler), including when
    the call drops abnormally — so it NEVER raises: any failure (missing
    dir, disk error, unreadable log) is printed as a warning, mirroring
    log_turn()'s own fail-safe contract. Returns the written Path, or None
    if nothing could be written.
    """
    try:
        _ensure_transcripts_dir()
        turns = _read_turns_for_call(call_id)

        model = llm_model or (turns[0].get("llm_model") if turns else None) or "unknown"
        started = started_at.isoformat() if hasattr(started_at, "isoformat") else (started_at or "unknown")
        ended = ended_at.isoformat() if hasattr(ended_at, "isoformat") else (ended_at or "unknown")

        lines = [
            f"# Call {call_id}",
            "",
            f"- Phone: {_mask_phone(phone)}",
            f"- Started: {started}",
            f"- Ended: {ended}",
            f"- LLM model: {model}",
            f"- Turns logged: {len(turns)}",
            "",
            "## Transcript",
            "",
        ]
        if not turns:
            lines.append("_No turns were logged for this call (it may have ended before any "
                          "completed turn, e.g. dropped during the opening line)._")
        for i, turn in enumerate(turns, start=1):
            lines.append(f"### Turn {i}")
            lines.append(f"**User:** {turn.get('user_text', '')}")
            lines.append(f"**Agent:** {turn.get('agent_text', '')}")
            metric_bits = []
            for key in ("stt_final_s", "ttft_s", "ttfa_s", "e2e_s"):
                value = turn.get(key)
                if value is not None:
                    metric_bits.append(f"{key}={value}s")
            if metric_bits:
                lines.append(f"_metrics: {', '.join(metric_bits)}_")
            lines.append("")

        path = TRANSCRIPTS_DIR / f"CALL_{call_id}.md"
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except Exception as exc:
        print(f"[!] Could not write call transcript for {call_id}: {exc}")
        return None
