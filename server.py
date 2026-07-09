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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import event_bus
import interviewer
import vz_logger
from call_launcher import start_tunnel, trigger_call
from cascade import CascadeSession, classify_error
from transports import BrowserTransport, TwilioTransport, parse_twilio_event
from vz_asr import transcribe_prerecorded
from vz_config import (
    AGENT_PROFILE,
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


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
                to_number = (value.get("custom_parameters") or {}).get("to")
                if session is None:
                    call_id = vz_logger.make_call_id(phone=to_number)
                    transport.call_id = call_id
                    session = CascadeSession(
                        transport=transport,
                        stt_cfg=TWILIO_STT_CFG,
                        llm_cfg={"model_id": AGENT_PROFILE.get("llm_model")},
                        tts_cfg=TWILIO_TTS_CFG,
                        profile=AGENT_PROFILE,
                        call_id=call_id,
                        channel="twilio",
                    )
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


async def _run_interview_turn(ws: WebSocket, session: "interviewer.InterviewSession", fn):
    """Runs one interviewer turn (fn(on_token) -> visible_text, a blocking
    interviewer.InterviewSession method) on a worker thread, forwarding LLM
    tokens live to the client as interviewer_token events, then sends
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
            await ws.send_json({"type": "interviewer_token", "token": item})

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
