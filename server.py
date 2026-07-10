"""
server.py — Voxniac ONE web app: FastAPI + WebSocket streaming voice agent.

Serves the static call UI and exposes:
  GET  /              -> static/index.html
  GET  /config         -> available LLM models + agent profile + defaults +
                          interviewer model choices (Phase 3.5 P2)
  WS   /ws/call         -> browser live-call protocol (PCM16 mic in / TTS out)
  WS   /ws/twilio       -> Twilio Media Streams (mulaw 8k in/out)
  POST /call            -> Phase 3: trigger a real outbound call ("call a prospect")
  GET  /call/status     -> Phase 3: is the server-managed cloudflared tunnel up?
  WS   /ws/interview    -> Phase 3: the onboarding interviewer (text chat, no audio)
  WS   /ws/monitor      -> Phase 3.5 P1: live fan-out of any Twilio call's events
  POST /interview/audio -> Phase 3.5 P2: voice-note setup (Deepgram pre-recorded
                          REST -> injected into the live /ws/interview session)
  POST /interviewer/model -> Phase 3.5 P2: switch/persist the interviewer's
                          model id (120B quality / 20B fast)
  POST /profile/reload  -> Phase 3.5 P3: force-reread agent_profile.json now
                          (for a hand edit made outside the interviewer UI —
                          interviewer.approve() already calls this itself)
  GET  /profile         -> Phase 4 Etapa B: current agent profile, shaped for
                          the Agent Setup "Profile Editor" (agent_opening,
                          prompt_blocks.*, truth_base, voice, llm_model)
  POST /profile         -> Phase 4 Etapa B: validate + write agent_profile.json
                          from the Profile Editor's manual fields, then
                          hot-reload via the same reload_agent_profile()
  POST /leads/import     -> Phase 4 Etapa C: import an Apollo-export-shaped
                          CSV (multipart "file" field) into leads.db, phone/
                          email obfuscated in-line (see leads.py) -> {"imported","skipped"}
  GET  /leads            -> Phase 4 Etapa C: list leads (?status=COLD|WARM|HOT)
  PATCH /leads/{id}      -> Phase 4 Etapa C: edit a lead's editable fields
  POST /leads/{id}/generate_prompt -> Phase 4 Etapa C: Fireworks gpt-oss-120b
                          drafts {"first_message","system_prompt"} for this
                          lead (grounded in the current agent_profile.json
                          persona), saved onto the lead
  POST /leads/{id}/call  -> Phase 4 Etapa C: places a call using the lead's
                          (or request body's) first_message/system_prompt as
                          an in-memory override of /ws/twilio's session —
                          agent_profile.json is never touched. DEMO_SAFE_MODE
                          (env var, default "true") always dials
                          CALL_ME_NUMBER, never a lead's own number (which is
                          masked at import time anyway — there is no real
                          number to dial).

Non-negotiable principles (see PLAN_ONE.md section 6, PLAN_FASE3.md section C):
- Fail loud: every provider failure is sent to the client with stage + detail,
  classified as auth/network/unknown (cascade.classify_error).
- A provider failure NEVER closes the WebSocket — the call stays alive and
  ready for the next turn (CascadeSession handles this internally; this file
  only guards against unexpected exceptions at the transport/session-lifecycle
  level so a bug there can't kill the socket either).
- Nothing hard-crashes the session.
- /ws/interview and /call never interfere with /ws/call or /ws/twilio — they
  are independent routes with their own session objects; nothing here is
  shared mutable state except AGENT_PROFILE (by design: that's the whole
  point of the hot-reload in vz_config.reload_agent_profile()).
"""

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import event_bus
import interviewer
import leads
import vz_logger
from call_launcher import start_tunnel, trigger_call
from cascade import CascadeSession, classify_error
from transports import BrowserTransport, TwilioTransport, parse_twilio_event
from vz_asr import transcribe_prerecorded
from vz_config import (
    AGENT_PROFILE,
    AGENT_PROFILE_PATH,
    INTERVIEWER_MODEL_CHOICES,
    engine_availability,
    get_interviewer_model_id,
    reload_agent_profile,
    set_interviewer_model_id,
)
from vz_llm import MODELOS_LLM

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("voxniac_one.server")

THIS_DIR = Path(__file__).resolve().parent
STATIC_DIR = THIS_DIR / "static"

BROWSER_STT_CFG = {"encoding": "linear16", "sample_rate": 16000}
BROWSER_TTS_CFG = {"encoding": "linear16", "sample_rate": 24000}
TWILIO_STT_CFG = {"encoding": "mulaw", "sample_rate": 8000}
TWILIO_TTS_CFG = {"encoding": "mulaw", "sample_rate": 8000}

# ---------------------------------------------------------------------------
# Phase 3 §A: server-managed cloudflared tunnel ("Call a prospect")
# ---------------------------------------------------------------------------
# The local port uvicorn is actually bound to. RUN_VOXNIAC_ONE.bat hardcodes
# --port 8080 on the CLI, which this process can't introspect from within, so
# it's mirrored here via an env var (defaults to 8080, unchanged behavior).
TUNNEL_PORT = int(os.environ.get("VOXNIAC_PORT", "8080"))
# Cloud deploys (or anywhere already reachable at a public hostname) can skip
# spawning cloudflared entirely by setting this to the public host, e.g.
# "voxniac-one.example.com" (no scheme).
PUBLIC_HOST_OVERRIDE = os.environ.get("VOXNIAC_PUBLIC_HOST")

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

_tunnel_process = None

# ---------------------------------------------------------------------------
# Phase 4 Etapa C: lead-call overrides + DEMO_SAFE_MODE
# ---------------------------------------------------------------------------
# Keyed by a one-shot uuid4 hex ("override_key", passed to Twilio as a custom
# Stream Parameter alongside "to" and "lead_id" — see call_launcher.
# trigger_call's extra_params). ws_twilio's "start" handler pops (reads +
# deletes) the entry for the incoming call's override_key the moment the
# call connects, so this dict never grows unbounded and never leaks a stale
# override into a later, unrelated call. Never persisted to disk — this is
# exactly the "without touching agent_profile.json" mechanism the spec asks
# for.
_LEAD_CALL_OVERRIDES: "dict[str, dict]" = {}


def _demo_safe_mode() -> bool:
    """Read live (never cached) so a test or an operator can flip
    DEMO_SAFE_MODE without restarting the process. Default true: absent or
    any value other than an explicit falsy string keeps the safety net on.
    NEVER reads/writes any .env file — this is a plain process environment
    variable, exactly like every other os.getenv() call in this codebase."""
    return os.getenv("DEMO_SAFE_MODE", "true").strip().lower() not in ("false", "0", "no", "off")


def _stop_tunnel_process():
    global _tunnel_process
    if _tunnel_process is None:
        return
    try:
        _tunnel_process.terminate()
        _tunnel_process.wait(timeout=5)
    except Exception:
        try:
            _tunnel_process.kill()
        except Exception as exc:
            logger.error("shutdown: could not kill the cloudflared tunnel process: %s", exc)
    _tunnel_process = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tunnel_process
    app.state.tunnel_up = False
    app.state.public_host = None
    app.state.public_wss = None

    # Clean up any orphan cloudflared processes from previous crashes
    _stop_tunnel_process()

    if PUBLIC_HOST_OVERRIDE:
        app.state.public_host = PUBLIC_HOST_OVERRIDE
        app.state.public_wss = f"wss://{PUBLIC_HOST_OVERRIDE}/ws/twilio"
        app.state.tunnel_up = True
        logger.info(
            "startup: VOXNIAC_PUBLIC_HOST override set (%s) — skipping cloudflared.",
            PUBLIC_HOST_OVERRIDE,
        )
    else:
        try:
            process, host = await asyncio.to_thread(start_tunnel, TUNNEL_PORT)
            _tunnel_process = process
            app.state.public_host = host
            app.state.public_wss = f"wss://{host}/ws/twilio"
            app.state.tunnel_up = True
            logger.info("startup: cloudflared tunnel is up at https://%s", host)
        except Exception as exc:
            # Fail loud in the logs, but never crash the server: browser calls
            # (/ws/call) don't need the tunnel at all, and phone calls will
            # correctly report "no tunnel" via /call/status and POST /call.
            logger.error(
                "startup: cloudflared tunnel failed to start (%s). Browser calls still "
                "work; phone calls (POST /call) will fail loud until this is fixed.",
                exc,
            )

    yield

    _stop_tunnel_process()


app = FastAPI(title="Voxniac ONE", lifespan=lifespan)


# ---------------------------------------------------------------------------
# /config
# ---------------------------------------------------------------------------
def _llm_options():
    available, reason = engine_availability()["fireworks"]
    options = []
    for _key, (model_id, label, reasoning_effort) in MODELOS_LLM.items():
        options.append({
            "key": model_id,
            "label": label,
            "reasoning_effort": reasoning_effort,
            "available": bool(available),
            "reason": None if available else reason,
        })
    return options


@app.get("/config")
def get_config():
    return JSONResponse({
        "llm": _llm_options(),
        "profile": {"agent_opening": AGENT_PROFILE.get("agent_opening", "")},
        "defaults": {
            "llm_model": AGENT_PROFILE.get("llm_model"),
            "voice": AGENT_PROFILE.get("voice"),
        },
        "interviewer": {
            "model_id": get_interviewer_model_id(),
            "choices": [
                {"key": model_id, "label": label}
                for model_id, label in INTERVIEWER_MODEL_CHOICES.items()
            ],
        },
        "demo_safe_mode": _demo_safe_mode(),
    })


# ---------------------------------------------------------------------------
# Phase 3.5 P2: POST /interviewer/model — switch the interviewer's LLM
# ---------------------------------------------------------------------------
class InterviewerModelRequest(BaseModel):
    model_id: str


@app.post("/interviewer/model")
def set_interviewer_model(payload: InterviewerModelRequest):
    ok = set_interviewer_model_id(payload.model_id)
    if not ok:
        return JSONResponse(
            {"error": {
                "stage": "interviewer_model",
                "kind": "bad_request",
                "detail": f"Unknown model_id '{payload.model_id}'. Valid choices: "
                          f"{', '.join(INTERVIEWER_MODEL_CHOICES)}",
            }},
            status_code=400,
        )
    return JSONResponse({"model_id": get_interviewer_model_id()})


# ---------------------------------------------------------------------------
# Phase 3.5 P3: POST /profile/reload — hot-reload agent_profile.json on demand
# ---------------------------------------------------------------------------
# interviewer.py's approve() already calls vz_config.reload_agent_profile()
# right after writing a new profile from the Agent Setup UI, so THAT path
# hot-reloads automatically. This endpoint exists for the OTHER case the
# acceptance criteria call out explicitly: the CEO hand-edits a block (e.g.
# "guardrails") directly in agent_profile.json with a text editor, bypassing
# the interviewer entirely — nothing re-reads the file from disk on its own
# in that case (AGENT_PROFILE is loaded once at process start), so this is
# the trigger. Same effect/semantics as reload_agent_profile()'s own
# docstring: the very next call picks up every field except the opening line
# and TTS voice for a call already in flight.
@app.post("/profile/reload")
def profile_reload():
    fresh = reload_agent_profile()
    return JSONResponse({"agent_opening": fresh.get("agent_opening", "")})


# ---------------------------------------------------------------------------
# Phase 4 Etapa B: GET/POST /profile — Agent Setup's "Profile Editor" (manual
# edit of agent_opening/prompt_blocks/truth_base/voice/llm_model), a second,
# independent way to write agent_profile.json besides the interviewer's own
# chat-driven Approve flow (interviewer.py's approve()). POST reuses the
# exact same vz_config.reload_agent_profile() hot-reload interviewer.approve()
# already calls, so a Profile Editor save takes effect on the very next
# call/turn identically — see reload_agent_profile()'s own docstring for the
# in-flight-call caveat (opening line + TTS voice already read stay as-is).
# ---------------------------------------------------------------------------
_PROMPT_BLOCK_KEYS = ("personality", "environment", "tone", "goal", "guardrails")


def _profile_editor_payload(profile: dict) -> dict:
    """Shapes an agent_profile.json-loaded dict (see vz_config.load_agent_
    profile/AGENT_PROFILE) into exactly what the Profile Editor's fields need:
    the 5 canonical prompt_blocks keys always present (never missing -> a
    legacy/partial profile still renders blank textareas instead of raising),
    truth_base as a plain dict (client JSON.stringifies it for its textarea)."""
    blocks = profile.get("prompt_blocks")
    if not isinstance(blocks, dict):
        blocks = {}
    truth_base = profile.get("truth_base")
    if not isinstance(truth_base, dict):
        truth_base = {}
    return {
        "agent_opening": profile.get("agent_opening", "") or "",
        "voice": profile.get("voice", "") or "",
        "llm_model": profile.get("llm_model", "") or "",
        "prompt_blocks": {key: (blocks.get(key, "") or "") for key in _PROMPT_BLOCK_KEYS},
        "truth_base": truth_base,
    }


def _profile_error(detail: str, status_code: int = 400):
    return JSONResponse(
        {"error": {"stage": "profile", "kind": "bad_request", "detail": detail}},
        status_code=status_code,
    )


@app.get("/profile")
def get_profile():
    return JSONResponse(_profile_editor_payload(AGENT_PROFILE))


@app.post("/profile")
async def set_profile(request: Request):
    """Validates the Profile Editor's payload field by field (never trusting
    a pydantic model's default 422 here — every failure mode the spec calls
    out, including an unparseable JSON body itself, comes back as a fail-loud
    HTTP 400 with a {"error":{"stage":"profile",...}} detail, same shape as
    every other endpoint in this file), writes agent_profile.json, and hot-
    reloads it via reload_agent_profile() — identical effect to interviewer.
    approve(), just triggered from the manual editor instead of the chat."""
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _profile_error(f"Invalid JSON body: {exc}")

    if not isinstance(payload, dict):
        return _profile_error("Payload must be a JSON object.")

    agent_opening = payload.get("agent_opening", "")
    if not isinstance(agent_opening, str):
        return _profile_error("'agent_opening' must be a string.")

    voice = payload.get("voice", "")
    if not isinstance(voice, str):
        return _profile_error("'voice' must be a string.")

    llm_model = payload.get("llm_model", "")
    if not isinstance(llm_model, str):
        return _profile_error("'llm_model' must be a string.")

    raw_blocks = payload.get("prompt_blocks", {})
    if not isinstance(raw_blocks, dict):
        return _profile_error("'prompt_blocks' must be a JSON object.")
    prompt_blocks = {}
    for key in _PROMPT_BLOCK_KEYS:
        value = raw_blocks.get(key, "")
        if not isinstance(value, str):
            return _profile_error(f"'prompt_blocks.{key}' must be a string.")
        prompt_blocks[key] = value

    raw_truth_base = payload.get("truth_base", {})
    if isinstance(raw_truth_base, str):
        stripped = raw_truth_base.strip()
        if not stripped:
            truth_base = {}
        else:
            try:
                truth_base = json.loads(stripped)
            except json.JSONDecodeError as exc:
                return _profile_error(f"'truth_base' is not valid JSON: {exc}")
            if not isinstance(truth_base, dict):
                return _profile_error("'truth_base' JSON must decode to an object.")
    elif isinstance(raw_truth_base, dict):
        truth_base = raw_truth_base
    else:
        return _profile_error("'truth_base' must be a JSON object or a JSON-encoded string.")

    profile = {
        "agent_opening": agent_opening,
        "voice": voice,
        "llm_model": llm_model,
        "prompt_blocks": prompt_blocks,
        "truth_base": truth_base,
    }

    try:
        with open(AGENT_PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error("POST /profile: could not write agent_profile.json: %s", exc)
        return JSONResponse(
            {"error": {"stage": "profile", "kind": "unknown", "detail": str(exc)[:200]}},
            status_code=500,
        )

    fresh = reload_agent_profile(AGENT_PROFILE_PATH)
    return JSONResponse(_profile_editor_payload(fresh))


# ---------------------------------------------------------------------------
# Phase 4 Etapa C: leads (Campaigns layer)
# ---------------------------------------------------------------------------
def _leads_error(detail: str, status_code: int = 400, kind: str = "bad_request"):
    return JSONResponse(
        {"error": {"stage": "leads", "kind": kind, "detail": detail}},
        status_code=status_code,
    )


@app.post("/leads/import")
async def import_leads(file: UploadFile = File(...)):
    """Multipart CSV upload (Apollo export shape, tolerant of missing/
    renamed columns — see leads.import_csv). Phone/email are obfuscated
    in-line at import time; the raw values from the upload are never
    persisted, logged, or echoed back in this response."""
    content = await file.read()
    if not content:
        return _leads_error("Empty file upload.")
    try:
        result = await asyncio.to_thread(leads.import_csv, content)
    except leads.LeadsError as exc:
        logger.error("POST /leads/import failed: %s", exc)
        return _leads_error(str(exc)[:300])
    return JSONResponse(result)


@app.get("/leads")
def get_leads(status: "str | None" = None):
    if status and status not in leads.STATUSES:
        return _leads_error(f"'status' must be one of {leads.STATUSES}, got '{status}'.")
    return JSONResponse(leads.list_leads(status))


@app.patch("/leads/{lead_id}")
async def patch_lead(lead_id: str, request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _leads_error(f"Invalid JSON body: {exc}")
    if not isinstance(payload, dict):
        return _leads_error("Payload must be a JSON object.")
    if "status" in payload and payload["status"] not in leads.STATUSES:
        return _leads_error(f"'status' must be one of {leads.STATUSES}.")
    if "painPoints" in payload and not isinstance(payload["painPoints"], list):
        return _leads_error("'painPoints' must be a JSON array.")

    updated = leads.update_lead(lead_id, payload)
    if updated is None:
        return _leads_error(f"Lead '{lead_id}' not found.", status_code=404, kind="not_found")
    return JSONResponse(updated)


@app.post("/leads/{lead_id}/generate_prompt")
async def generate_lead_prompt_endpoint(lead_id: str):
    """Fireworks gpt-oss-120b (same client/pattern as interviewer.py's plan
    drafter — see leads.generate_lead_prompt) drafts a personalized
    first_message/system_prompt for this lead, grounded in the CURRENT
    agent_profile.json persona, and saves it onto the lead."""
    lead = leads.get_lead(lead_id)
    if lead is None:
        return _leads_error(f"Lead '{lead_id}' not found.", status_code=404, kind="not_found")

    try:
        result = await asyncio.to_thread(leads.generate_lead_prompt, lead, AGENT_PROFILE)
    except leads.LeadsError as exc:
        kind = classify_error(exc)
        logger.error("POST /leads/%s/generate_prompt failed (%s): %s", lead_id, kind, exc)
        return JSONResponse(
            {"error": {"stage": "leads", "kind": kind, "detail": str(exc)[:200]}},
            status_code=502,
        )

    updated = leads.update_lead_generated_prompt(
        lead_id, result["first_message"], result["system_prompt"]
    )
    return JSONResponse(updated)


@app.post("/leads/{lead_id}/call")
async def call_lead(lead_id: str, request: Request):
    """Places a call for this lead through the SAME /ws/twilio pipeline as
    POST /call, with an optional first_message/system_prompt override that
    lives ONLY in the in-memory _LEAD_CALL_OVERRIDES dict for the duration
    of this one call — agent_profile.json is never read or written here.

    DEMO_SAFE_MODE (default true): this codebase never stores a lead's real
    phone number at all (leads.py masks it in-line at import time), so every
    lead call already has no real number to dial — CALL_ME_NUMBER is used
    unconditionally. DEMO_SAFE_MODE is still read and reported in the
    response so the flag has real, visible effect the moment a future real
    lead-number source (see scrapers/__init__.py) is wired in.
    """
    lead = leads.get_lead(lead_id)
    if lead is None:
        return _leads_error(f"Lead '{lead_id}' not found.", status_code=404, kind="not_found")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    first_message = lead.get("customFirstMessage") or ""
    system_prompt = lead.get("customSystemPrompt") or ""
    if isinstance(body.get("first_message"), str) and body["first_message"].strip():
        first_message = body["first_message"]
    if isinstance(body.get("system_prompt"), str) and body["system_prompt"].strip():
        system_prompt = body["system_prompt"]

    demo_safe = _demo_safe_mode()
    if not demo_safe:
        logger.warning(
            "POST /leads/%s/call: DEMO_SAFE_MODE is off, but this codebase never stores a "
            "lead's real phone number (masked at import) — still dialing CALL_ME_NUMBER.",
            lead_id,
        )

    to = (os.environ.get("CALL_ME_NUMBER") or "").strip()
    if not to or not E164_RE.match(to):
        return JSONResponse(
            {"error": {
                "stage": "call",
                "kind": "unknown",
                "detail": "CALL_ME_NUMBER is not set to a valid E.164 number — cannot place "
                          "any lead call (every lead call dials CALL_ME_NUMBER; leads never "
                          "carry a real, dialable number).",
            }},
            status_code=502,
        )

    public_wss = getattr(app.state, "public_wss", None)
    if not public_wss:
        return JSONResponse(
            {"error": {
                "stage": "tunnel",
                "kind": "unknown",
                "detail": "No public tunnel is available (cloudflared failed to start "
                          "and VOXNIAC_PUBLIC_HOST is not set) — cannot place a phone call.",
            }},
            status_code=502,
        )

    override_key = uuid.uuid4().hex
    if first_message or system_prompt:
        _LEAD_CALL_OVERRIDES[override_key] = {
            "lead_id": lead_id,
            "first_message": first_message,
            "system_prompt": system_prompt,
        }

    try:
        sid = await asyncio.to_thread(
            trigger_call, to, public_wss, {"lead_id": lead_id, "override_key": override_key},
        )
    except Exception as exc:
        _LEAD_CALL_OVERRIDES.pop(override_key, None)
        kind = classify_error(exc)
        logger.error("POST /leads/%s/call: trigger_call failed (%s stage=call): %s", lead_id, kind, exc)
        return JSONResponse(
            {"error": {"stage": "call", "kind": kind, "detail": str(exc)[:200]}},
            status_code=502,
        )

    return JSONResponse({
        "sid": sid, "to": to, "status": "queued", "lead_id": lead_id, "demo_safe_mode": demo_safe,
    })


async def _classify_lead_after_call(lead_id: str, call_id: str):
    """Fire-and-forget (called from ws_twilio's teardown `finally` block via
    asyncio.create_task — never awaited there, so it can't delay closing the
    socket): runs Fireworks classification (leads.classify_lead_call) on the
    finished call's transcript and updates the lead's status +
    classificationReasoning. Any failure is logged and the lead's status is
    left exactly as it was — never guesses, never crashes the server."""
    try:
        lead = await asyncio.to_thread(leads.get_lead, lead_id)
        if lead is None:
            return
        transcript_text = await asyncio.to_thread(vz_logger.get_call_transcript_text, call_id)
        result = await asyncio.to_thread(leads.classify_lead_call, lead, transcript_text)
        await asyncio.to_thread(
            leads.update_lead_classification, lead_id, result["status"], result["reasoning"],
        )
    except Exception as exc:
        logger.error(
            "classify_lead_after_call: failed for lead=%s call=%s: %s", lead_id, call_id, exc
        )


# ---------------------------------------------------------------------------
# Phase 3 §A: POST /call, GET /call/status
# ---------------------------------------------------------------------------
class CallRequest(BaseModel):
    to: str


@app.get("/call/status")
def call_status():
    return JSONResponse({
        "tunnel_up": bool(getattr(app.state, "tunnel_up", False)),
        "public_host": getattr(app.state, "public_host", None),
    })


@app.post("/tunnel/restart")
async def restart_tunnel_endpoint():
    """Phase 4: manually trigger a tunnel restart if it's down."""
    global _tunnel_process
    _stop_tunnel_process()
    
    if PUBLIC_HOST_OVERRIDE:
        return JSONResponse({"status": "ignored", "detail": "VOXNIAC_PUBLIC_HOST is set, skipping tunnel."})
        
    try:
        process, host = await asyncio.to_thread(start_tunnel, TUNNEL_PORT)
        _tunnel_process = process
        app.state.public_host = host
        app.state.public_wss = f"wss://{host}/ws/twilio"
        app.state.tunnel_up = True
        return JSONResponse({"status": "ok", "public_host": host})
    except Exception as exc:
        app.state.tunnel_up = False
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@app.post("/call")
async def call_prospect(payload: CallRequest):
    to = (payload.to or "").strip()
    if not E164_RE.match(to):
        return JSONResponse(
            {"error": {
                "stage": "validation",
                "kind": "bad_request",
                "detail": f"'{to}' is not a valid E.164 phone number, e.g. +13075550100",
            }},
            status_code=400,
        )

    public_wss = getattr(app.state, "public_wss", None)
    if not public_wss:
        return JSONResponse(
            {"error": {
                "stage": "tunnel",
                "kind": "unknown",
                "detail": "No public tunnel is available (cloudflared failed to start "
                          "and VOXNIAC_PUBLIC_HOST is not set) — cannot place a phone call.",
            }},
            status_code=502,
        )

    try:
        sid = await asyncio.to_thread(trigger_call, to, public_wss)
    except Exception as exc:
        kind = classify_error(exc)
        logger.error("POST /call: trigger_call failed (%s stage=call): %s", kind, exc)
        return JSONResponse(
            {"error": {"stage": "call", "kind": kind, "detail": str(exc)[:200]}},
            status_code=502,
        )

    return JSONResponse({"sid": sid, "to": to, "status": "queued"})


# ---------------------------------------------------------------------------
# Static + index
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


class NoCacheStaticFiles(StaticFiles):
    """Forces `Cache-Control: no-cache` on every /static response so the
    browser always revalidates with the server (a cheap 304 via the
    ETag/Last-Modified StaticFiles already sets) instead of serving a stale
    app.js/style.css from its heuristic cache with no request at all — the
    exact bug that showed the CEO an old UI during the hackathon demo.
    Subclassing is the minimal fix: no new middleware, no new dependency."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Error helper (top-level guard; CascadeSession already sends its own error
# events for provider failures during a call — this covers unexpected bugs at
# the WS-handling level so those can't kill the socket either).
# ---------------------------------------------------------------------------
async def _send_error(ws: WebSocket, stage: str, exc: Exception):
    kind = classify_error(exc)
    try:
        await ws.send_json({
            "type": "error",
            "stage": stage,
            "kind": kind,
            "detail": str(exc)[:200],
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# WS /ws/call — browser live-call protocol
# ---------------------------------------------------------------------------
@app.websocket("/ws/call")
async def ws_call(ws: WebSocket):
    await ws.accept()
    session: "CascadeSession | None" = None

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            if "bytes" in msg and msg["bytes"] is not None:
                if session is not None:
                    await session.feed_audio(msg["bytes"])
                continue

            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")

                if mtype == "start_call":
                    if session is not None:
                        continue  # already in a call on this socket
                    model_id = data.get("llm") or AGENT_PROFILE.get("llm_model")
                    transport = BrowserTransport(ws)
                    # Phase 3.5 P1: call_id/channel tag every turn logged for
                    # this call in voxniac_one_log.jsonl (see cascade.py's
                    # log_turn call) — no transcript file for browser calls
                    # (that's scoped to Twilio, see ws_twilio below).
                    call_id = vz_logger.make_call_id(prefix="browser")
                    session = CascadeSession(
                        transport=transport,
                        stt_cfg=BROWSER_STT_CFG,
                        llm_cfg={"model_id": model_id},
                        tts_cfg=BROWSER_TTS_CFG,
                        profile=AGENT_PROFILE,
                        call_id=call_id,
                        channel="browser",
                    )
                    await ws.send_json({
                        "type": "call_started",
                        "opening": AGENT_PROFILE.get("agent_opening", ""),
                    })
                    try:
                        await session.start()
                    except Exception as exc:
                        logger.error("ws_call: session.start() failed unexpectedly: %s", exc)
                        await _send_error(ws, "session", exc)

                elif mtype == "end_call":
                    if session is not None:
                        await session.stop()
                        session = None

    except WebSocketDisconnect:
        pass
    finally:
        if session is not None:
            await session.stop()


# ---------------------------------------------------------------------------
# WS /ws/twilio — Twilio Media Streams
# ---------------------------------------------------------------------------
@app.websocket("/ws/twilio")
async def ws_twilio(ws: WebSocket):
    """
    Phase 3.5 P1: the CascadeSession (and its call_id) can only be created
    once Twilio's "start" event arrives carrying the "to" custom Stream
    Parameter (see transports.parse_twilio_event / call_launcher.py) — so
    transport is built immediately at accept() but session stays None until
    then, same as before "started" already gated feed_audio(). On the way
    out (any exit path — clean "stop", disconnect, or an unexpected
    exception), the `finally` block ALWAYS tears the session down and, if a
    call_id was assigned, writes transcripts/CALL_<call_id>.md and notifies
    any UI monitor that the call ended — never losing the transcript.
    """
    await ws.accept()
    transport = TwilioTransport(ws)
    session: "CascadeSession | None" = None
    call_id: "str | None" = None
    to_number: "str | None" = None
    # Phase 4 Etapa C: set from the "start" event's custom Stream Parameters
    # (see call_launcher.trigger_call's extra_params) only for a call placed
    # via POST /leads/{id}/call — a regular POST /call prospect call never
    # carries these, so both stay None and every line below that guards on
    # `if lead_id:` is a no-op, exactly like before this feature existed.
    lead_id: "str | None" = None
    started_at = datetime.now(timezone.utc)

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" not in msg or msg["text"] is None:
                continue
            try:
                raw = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue

            kind, value = parse_twilio_event(raw)
            if kind == "connected":
                continue
            elif kind == "start":
                transport.stream_sid = value.get("stream_sid")
                custom_parameters = value.get("custom_parameters") or {}
                to_number = custom_parameters.get("to")
                lead_id = custom_parameters.get("lead_id") or None
                override_key = custom_parameters.get("override_key")
                if session is None:
                    call_id = vz_logger.make_call_id(phone=to_number)
                    transport.call_id = call_id

                    # Phase 4 Etapa C: apply this call's one-shot lead
                    # override (if any) WITHOUT ever touching AGENT_PROFILE
                    # or agent_profile.json — a shallow copy carries the
                    # overridden opening line (session.profile.get(
                    # "agent_opening") -> start()'s spoken opening), and
                    # system_prompt_override is passed straight through to
                    # CascadeSession so the LIVE conversation turns use it
                    # too (see cascade.py's _run_turn). Every other field
                    # (voice, llm_model, prompt_blocks/truth_base fallback)
                    # is untouched, still read from the real, hot-reloadable
                    # global profile.
                    session_profile = AGENT_PROFILE
                    system_prompt_override = None
                    if override_key:
                        override = _LEAD_CALL_OVERRIDES.pop(override_key, None)
                        if override:
                            session_profile = dict(AGENT_PROFILE)
                            if override.get("first_message"):
                                session_profile["agent_opening"] = override["first_message"]
                            if override.get("system_prompt"):
                                system_prompt_override = override["system_prompt"]

                    session_kwargs = dict(
                        transport=transport,
                        stt_cfg=TWILIO_STT_CFG,
                        llm_cfg={"model_id": AGENT_PROFILE.get("llm_model")},
                        tts_cfg=TWILIO_TTS_CFG,
                        profile=session_profile,
                        call_id=call_id,
                        channel="twilio",
                    )
                    if system_prompt_override:
                        session_kwargs["system_prompt_override"] = system_prompt_override
                    session = CascadeSession(**session_kwargs)
                    try:
                        await session.start()
                    except Exception as exc:
                        logger.error("ws_twilio: session.start() failed unexpectedly: %s", exc)
            elif kind == "media":
                if session is not None and value:
                    await session.feed_audio(value)
            elif kind == "stop":
                break

    except WebSocketDisconnect:
        pass
    finally:
        if session is not None:
            await session.stop()
        if call_id is not None:
            ended_at = datetime.now(timezone.utc)
            # Fire-and-forget: lets any open "Call a prospect" monitor show
            # "colgó" (call ended) instead of leaving the last turn's status
            # stuck on "in call".
            event_bus.publish("twilio", call_id, {"type": "call_ended"})
            vz_logger.write_call_transcript(
                call_id, to_number, started_at, ended_at, AGENT_PROFILE.get("llm_model"),
            )
            if lead_id:
                # Cheap synchronous DB write (the call happened, regardless
                # of whether classification below succeeds or even runs).
                leads.update_lead_call(lead_id, call_id, ended_at.isoformat())
                # Fire-and-forget: classification runs a Fireworks call, so
                # it must never delay this WS handler's own teardown.
                asyncio.create_task(_classify_lead_after_call(lead_id, call_id))


# ---------------------------------------------------------------------------
# Phase 3.5 P1: WS /ws/monitor — live fan-out of any Twilio call's events
# ---------------------------------------------------------------------------
@app.websocket("/ws/monitor")
async def ws_monitor(ws: WebSocket):
    """Every envelope published via event_bus.publish() (currently: every
    event a live Twilio call's TwilioTransport.send_event() sees, wrapped as
    {"channel","call_id","event"}) is forwarded to this socket verbatim.
    Purely additive/read-only — never touches a call's own session or
    transport, so it can never affect audio latency or call state."""
    await ws.accept()
    queue = event_bus.subscribe()
    try:
        while True:
            envelope = await queue.get()
            try:
                await ws.send_json(envelope)
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(queue)


# ---------------------------------------------------------------------------
# Phase 3 §B: WS /ws/interview — the onboarding interviewer (text chat)
# ---------------------------------------------------------------------------
# Phase 3.5 P2: the single currently-open /ws/interview connection (this app
# is single-tenant — one CEO doing setup in one browser tab at a time — so a
# single shared slot is enough, not a general multi-session registry). Lets
# the stateless POST /interview/audio handler push a transcribed voice note
# into the SAME live interview session and stream the interviewer's reply
# back over the WS the browser already has open, per PLAN_FASE3_5.md P2.2's
# "the response flows through the existing /ws/interview WS" requirement.
_active_interview_ws: "WebSocket | None" = None
_active_interview_session: "interviewer.InterviewSession | None" = None


class StreamingThoughtParser:
    def __init__(self):
        self.in_thought = False
        self.buffer = ""

    def feed(self, token: str):
        self.buffer += token
        emitted = []
        
        while self.buffer:
            if not self.in_thought:
                start_idx = self.buffer.find("<think>")
                if start_idx != -1:
                    if start_idx > 0:
                        emitted.append(("token", self.buffer[:start_idx]))
                    self.in_thought = True
                    self.buffer = self.buffer[start_idx + len("<think>"):]
                else:
                    # Check if buffer could be a partial match of <think> (e.g., "<", "<t", "<th", etc.)
                    partial_match = False
                    for i in range(1, len("<think>")):
                        if "<think>".startswith(self.buffer[-i:]):
                            partial_match = True
                            break
                    if partial_match:
                        keep_len = i
                        emit_text = self.buffer[:-keep_len]
                        if emit_text:
                            emitted.append(("token", emit_text))
                        self.buffer = self.buffer[-keep_len:]
                        break
                    else:
                        emitted.append(("token", self.buffer))
                        self.buffer = ""
            else:
                end_idx = self.buffer.find("</think>")
                if end_idx != -1:
                    if end_idx > 0:
                        emitted.append(("thought", self.buffer[:end_idx]))
                    self.in_thought = False
                    self.buffer = self.buffer[end_idx + len("</think>"):]
                else:
                    # Check if buffer could be a partial match of </think> (e.g., "</", "</t", "</th", etc.)
                    partial_match = False
                    for i in range(1, len("</think>")):
                        if "</think>".startswith(self.buffer[-i:]):
                            partial_match = True
                            break
                    if partial_match:
                        keep_len = i
                        emit_text = self.buffer[:-keep_len]
                        if emit_text:
                            emitted.append(("thought", emit_text))
                        self.buffer = self.buffer[-keep_len:]
                        break
                    else:
                        emitted.append(("thought", self.buffer))
                        self.buffer = ""
        return emitted


async def _run_interview_turn(ws: WebSocket, session: "interviewer.InterviewSession", fn):
    """Runs one interviewer turn (fn(on_token) -> visible_text, a blocking
    interviewer.InterviewSession method) on a worker thread, forwarding LLM
    tokens live to the client as interviewer_token or interviewer_thought events, then sends
    interviewer_done (or a fail-loud error) followed by the fresh state
    snapshot. Mirrors cascade.py's run_in_executor + threadsafe-queue pattern
    used for the voice LLM turn."""
    loop = asyncio.get_event_loop()
    token_queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()

    def on_token(delta):
        loop.call_soon_threadsafe(token_queue.put_nowait, delta)

    def worker():
        try:
            result = fn(on_token)
            loop.call_soon_threadsafe(token_queue.put_nowait, ("__done__", result))
        except interviewer.InterviewerError as exc:
            loop.call_soon_threadsafe(token_queue.put_nowait, ("__error__", str(exc)))
        except Exception as exc:
            logger.error("ws_interview: unexpected turn failure: %s", exc)
            loop.call_soon_threadsafe(token_queue.put_nowait, ("__error__", str(exc)))
        finally:
            loop.call_soon_threadsafe(token_queue.put_nowait, SENTINEL)

    loop.run_in_executor(None, worker)

    parser = StreamingThoughtParser()
    final_text = None
    error_detail = None
    while True:
        item = await token_queue.get()
        if item is SENTINEL:
            break
        if isinstance(item, tuple) and item[0] == "__done__":
            final_text = item[1]
        elif isinstance(item, tuple) and item[0] == "__error__":
            error_detail = item[1]
        else:
            emitted = parser.feed(item)
            for kind, text in emitted:
                if kind == "thought":
                    await ws.send_json({"type": "interviewer_thought", "token": text})
                else:
                    await ws.send_json({"type": "interviewer_token", "token": text})

    if parser.buffer:
        if parser.in_thought:
            await ws.send_json({"type": "interviewer_thought", "token": parser.buffer})
        else:
            await ws.send_json({"type": "interviewer_token", "token": parser.buffer})

    if error_detail is not None:
        await ws.send_json({
            "type": "error",
            "stage": "interviewer",
            "kind": classify_error(RuntimeError(error_detail)),
            "detail": error_detail[:200],
        })
    else:
        await ws.send_json({"type": "interviewer_done", "text": final_text or ""})

    await ws.send_json(session.snapshot())


@app.websocket("/ws/interview")
async def ws_interview(ws: WebSocket):
    global _active_interview_ws, _active_interview_session
    await ws.accept()
    session = interviewer.InterviewSession()
    _active_interview_ws = ws
    _active_interview_session = session

    await ws.send_json(session.snapshot())
    if session.is_fresh():
        await _run_interview_turn(ws, session, session.opening_turn)

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" not in msg or msg["text"] is None:
                continue
            try:
                data = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue
            mtype = data.get("type")

            if mtype == "user_msg":
                text = data.get("text", "")
                await _run_interview_turn(ws, session, lambda on_token, t=text: session.user_turn(t, on_token))

            elif mtype == "approve":
                try:
                    session.approve()
                    await ws.send_json({"type": "profile_written"})
                except interviewer.InterviewerError as exc:
                    await _send_error(ws, "interviewer", exc)
                await ws.send_json(session.snapshot())

            elif mtype == "back":
                try:
                    session.back()
                except interviewer.InterviewerError as exc:
                    await _send_error(ws, "interviewer", exc)
                await ws.send_json(session.snapshot())

            elif mtype == "reset":
                session.reset()
                await ws.send_json(session.snapshot())
                await _run_interview_turn(ws, session, session.opening_turn)

    except WebSocketDisconnect:
        pass
    finally:
        if _active_interview_ws is ws:
            _active_interview_ws = None
            _active_interview_session = None


# ---------------------------------------------------------------------------
# Phase 3.5 P2: POST /interview/audio — voice-note setup
# ---------------------------------------------------------------------------
@app.post("/interview/audio")
async def interview_audio(request: Request):
    """
    Accepts a recorded voice note (raw body — the browser's MediaRecorder
    blob, typically webm/opus; Content-Type is read from the request header,
    not assumed), transcribes it via Deepgram pre-recorded REST
    (vz_asr.transcribe_prerecorded), and injects the resulting text into the
    currently open /ws/interview session exactly like a typed user_msg — the
    interviewer's reply streams back over that same WebSocket as
    interviewer_token/interviewer_done events (see _active_interview_ws
    above). Returns {"transcript": "..."} so the client can echo the user's
    own bubble immediately, without waiting on the LLM reply.

    Raw-body (not multipart form) by design: avoids adding python-multipart
    as a new pip dependency for something a plain POST body already covers.
    """
    if _active_interview_ws is None or _active_interview_session is None:
        return JSONResponse(
            {"error": {
                "stage": "interview_audio",
                "kind": "unknown",
                "detail": "No active Agent Setup session — open the Agent Setup panel first.",
            }},
            status_code=409,
        )

    audio_bytes = await request.body()
    if not audio_bytes:
        return JSONResponse(
            {"error": {"stage": "interview_audio", "kind": "unknown", "detail": "Empty audio upload."}},
            status_code=400,
        )

    content_type = request.headers.get("content-type") or "audio/webm"
    try:
        text = await asyncio.to_thread(transcribe_prerecorded, audio_bytes, content_type)
    except Exception as exc:
        kind = classify_error(exc)
        logger.error("POST /interview/audio: Deepgram transcription failed (%s): %s", kind, exc)
        return JSONResponse(
            {"error": {"stage": "interview_audio", "kind": kind, "detail": str(exc)[:200]}},
            status_code=502,
        )

    text = (text or "").strip()
    if not text:
        return JSONResponse(
            {"error": {
                "stage": "interview_audio",
                "kind": "unknown",
                "detail": "Deepgram returned an empty transcript — try speaking a bit longer/clearer.",
            }},
            status_code=422,
        )

    ws = _active_interview_ws
    session = _active_interview_session
    await _run_interview_turn(ws, session, lambda on_token, t=text: session.user_turn(t, on_token))

    return JSONResponse({"transcript": text})
