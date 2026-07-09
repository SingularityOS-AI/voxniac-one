"""Integration test for server.py's /ws/twilio route (Phase 3.5 P1):
call_id assignment from the "to" custom Stream Parameter, and the
transcript write on session teardown — all with CascadeSession replaced by
a network-free fake (real CascadeSession.start()/stop() would try to open
real Deepgram WebSockets). No network, no real Twilio/Fireworks/Deepgram
calls anywhere in this test.
"""

import base64

import pytest
from fastapi.testclient import TestClient

import server
import vz_config


@pytest.fixture(autouse=True)
def _skip_real_cloudflared_tunnel(monkeypatch):
    """This dev machine actually HAS a working cloudflared.exe at server.py's
    hardcoded default path, so without this override every TestClient(app)
    lifespan startup below would spawn a REAL cloudflared process and reach
    out to Cloudflare's network — exactly what "no llamadas de red reales"
    forbids. Forcing the VOXNIAC_PUBLIC_HOST override path (server.py
    reads it once at import time into PUBLIC_HOST_OVERRIDE) makes lifespan
    skip cloudflared entirely, deterministically, with zero network I/O."""
    monkeypatch.setattr(server, "PUBLIC_HOST_OVERRIDE", "test.invalid")


class FakeCascadeSession:
    """Matches CascadeSession's constructor signature exactly; start()/stop()/
    feed_audio() are no-op async methods so no real STT/TTS/LLM network call
    is ever made."""

    instances = []

    def __init__(self, transport, stt_cfg, llm_cfg, tts_cfg, profile, call_id=None, channel="unknown"):
        self.transport = transport
        self.call_id = call_id
        self.channel = channel
        self.started = False
        self.stopped = False
        self.fed_chunks = []
        FakeCascadeSession.instances.append(self)

    async def start(self):
        self.started = True

    async def feed_audio(self, chunk):
        self.fed_chunks.append(chunk)

    async def stop(self):
        self.stopped = True


def test_ws_twilio_assigns_call_id_and_writes_transcript_on_stop(monkeypatch):
    FakeCascadeSession.instances.clear()
    monkeypatch.setattr(server, "CascadeSession", FakeCascadeSession)

    written = {}

    def fake_write_call_transcript(call_id, phone, started_at, ended_at, llm_model=None):
        written["call_id"] = call_id
        written["phone"] = phone
        written["llm_model"] = llm_model
        return None

    monkeypatch.setattr(server.vz_logger, "write_call_transcript", fake_write_call_transcript)

    published = []
    monkeypatch.setattr(server.event_bus, "publish", lambda channel, call_id, event: published.append(
        (channel, call_id, event)
    ))

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws/twilio") as ws:
            ws.send_json({"event": "connected"})
            ws.send_json({
                "event": "start",
                "start": {
                    "streamSid": "MZ123",
                    "callSid": "CA123",
                    "customParameters": {"to": "+13075550100"},
                },
            })
            media_payload = base64.b64encode(b"\x00" * 160).decode()
            ws.send_json({"event": "media", "media": {"payload": media_payload}})
            ws.send_json({"event": "stop"})
        # Exiting the `with` block closes the client side of the socket;
        # server.py's `finally` block runs regardless.

    assert len(FakeCascadeSession.instances) == 1
    session = FakeCascadeSession.instances[0]
    assert session.channel == "twilio"
    assert session.call_id is not None
    assert session.call_id.endswith("_0100")  # last 4 digits of +13075550100
    assert session.started is True
    assert session.stopped is True
    assert len(session.fed_chunks) == 1

    assert written["call_id"] == session.call_id
    assert written["phone"] == "+13075550100"

    call_ended = [p for p in published if p[2].get("type") == "call_ended"]
    assert call_ended and call_ended[0][0] == "twilio" and call_ended[0][1] == session.call_id


def test_ws_twilio_without_custom_parameters_falls_back_to_0000(monkeypatch):
    """Legacy TwiML (no <Parameter name="to">, e.g. an older deployed
    dialer) must not crash — call_id just falls back to the "0000" suffix."""
    FakeCascadeSession.instances.clear()
    monkeypatch.setattr(server, "CascadeSession", FakeCascadeSession)
    monkeypatch.setattr(server.vz_logger, "write_call_transcript", lambda *a, **k: None)
    monkeypatch.setattr(server.event_bus, "publish", lambda *a, **k: None)

    with TestClient(server.app) as client:
        with client.websocket_connect("/ws/twilio") as ws:
            ws.send_json({"event": "start", "start": {"streamSid": "MZ999"}})
            ws.send_json({"event": "stop"})

    session = FakeCascadeSession.instances[-1]
    assert session.call_id.endswith("_0000")


def test_get_config_exposes_interviewer_choices():
    with TestClient(server.app) as client:
        resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "interviewer" in data
    assert data["interviewer"]["model_id"]
    assert len(data["interviewer"]["choices"]) == 2


def test_post_interviewer_model_rejects_unknown_model():
    with TestClient(server.app) as client:
        resp = client.post("/interviewer/model", json={"model_id": "not-a-real-model"})
    assert resp.status_code == 400
    assert resp.json()["error"]["stage"] == "interviewer_model"


def test_static_assets_are_served_with_no_cache_header():
    """Regression test: the CEO saw a stale UI during the hackathon demo
    because the browser's heuristic cache served an old app.js/style.css
    with zero requests. Cache-Control: no-cache forces revalidation on
    every load (a cheap 304 via StaticFiles' own ETag/Last-Modified)."""
    with TestClient(server.app) as client:
        resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


def test_post_interview_audio_without_active_session_fails_loud():
    with TestClient(server.app) as client:
        resp = client.post(
            "/interview/audio",
            content=b"fake-audio-bytes",
            headers={"Content-Type": "audio/webm"},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["stage"] == "interview_audio"


class FakeInterviewWs:
    """Duck-type for the part of a WebSocket _run_interview_turn actually
    calls: async send_json(dict). Records everything sent."""

    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


class FakeInterviewSession:
    """Duck-type for interviewer.InterviewSession: only user_turn() (called
    by the POST /interview/audio handler exactly like a typed user_msg) and
    snapshot() (called by _run_interview_turn afterwards) are needed."""

    def __init__(self):
        self.received_texts = []

    def user_turn(self, text, on_token):
        self.received_texts.append(text)
        if on_token:
            on_token("Got it, next question: ")
            on_token("what's your pricing?")
        return "Got it, next question: what's your pricing?"

    def snapshot(self):
        return {"type": "state", "state": "INTERVIEWING", "fields": {}, "messages": [], "draft": None}


def test_post_interview_audio_happy_path_injects_transcript_and_streams_reply(monkeypatch):
    """Phase 3.5 P2 acceptance: a recorded voice note gets transcribed and
    injected into the live /ws/interview session exactly like a typed
    user_msg, and the interviewer's reply streams back over that same
    (fake) WebSocket as interviewer_token/interviewer_done events."""
    fake_ws = FakeInterviewWs()
    fake_session = FakeInterviewSession()
    monkeypatch.setattr(server, "_active_interview_ws", fake_ws)
    monkeypatch.setattr(server, "_active_interview_session", fake_session)

    def fake_transcribe_prerecorded(audio_bytes, content_type):
        assert audio_bytes == b"fake-webm-audio-bytes"
        assert content_type == "audio/webm;codecs=opus"
        return "  Tell me about your services and prices.  "

    monkeypatch.setattr(server, "transcribe_prerecorded", fake_transcribe_prerecorded)

    with TestClient(server.app) as client:
        resp = client.post(
            "/interview/audio",
            content=b"fake-webm-audio-bytes",
            headers={"Content-Type": "audio/webm;codecs=opus"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"transcript": "Tell me about your services and prices."}

    # The transcript was injected into the SAME session as a real user turn.
    assert fake_session.received_texts == ["Tell me about your services and prices."]

    # The interviewer's reply streamed back over the existing WS connection.
    token_events = [m for m in fake_ws.sent if m.get("type") == "interviewer_token"]
    done_events = [m for m in fake_ws.sent if m.get("type") == "interviewer_done"]
    assert "".join(e["token"] for e in token_events) == "Got it, next question: what's your pricing?"
    assert done_events and done_events[0]["text"] == "Got it, next question: what's your pricing?"
    state_events = [m for m in fake_ws.sent if m.get("type") == "state"]
    assert state_events  # snapshot sent last, per _run_interview_turn


def test_post_interview_audio_empty_body_fails_loud(monkeypatch):
    # Needs an active session first, otherwise the "no active session" (409)
    # guard fires before the empty-body (400) check even runs.
    monkeypatch.setattr(server, "_active_interview_ws", FakeInterviewWs())
    monkeypatch.setattr(server, "_active_interview_session", FakeInterviewSession())

    with TestClient(server.app) as client:
        resp = client.post("/interview/audio", content=b"", headers={"Content-Type": "audio/webm"})
    assert resp.status_code == 400
    assert resp.json()["error"]["stage"] == "interview_audio"


def test_post_interviewer_model_accepts_valid_model_id(tmp_path, monkeypatch):
    # set_interviewer_model_id/get_interviewer_model_id are imported by name
    # into server.py, but they still resolve CONFIG_JSON_PATH/
    # INTERVIEWER_CONFIG via vz_config's OWN module globals at call time —
    # so patching them here (on the vz_config module itself) is what
    # actually takes effect, regardless of how server.py imported the names.
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(vz_config, "INTERVIEWER_CONFIG", dict(vz_config.INTERVIEWER_CONFIG))
    monkeypatch.setattr(vz_config, "CONFIG_JSON_PATH", config_path)
    monkeypatch.delenv("INTERVIEWER_MODEL_ID", raising=False)

    target = "accounts/fireworks/models/gpt-oss-20b"
    with TestClient(server.app) as client:
        resp = client.post("/interviewer/model", json={"model_id": target})

    assert resp.status_code == 200
    assert resp.json() == {"model_id": target}
    assert config_path.exists()  # persisted to disk, per set_interviewer_model_id


def test_post_profile_reload_rereads_agent_profile_json(monkeypatch):
    """Phase 3.5 P3 acceptance: a hand edit to agent_profile.json (bypassing
    the interviewer UI entirely) must be picked up without restarting the
    server, via this endpoint."""
    called = {}

    def fake_reload():
        called["ran"] = True
        return {"agent_opening": "Reloaded opening line."}

    monkeypatch.setattr(server, "reload_agent_profile", fake_reload)

    with TestClient(server.app) as client:
        resp = client.post("/profile/reload")

    assert resp.status_code == 200
    assert called.get("ran") is True
    assert resp.json()["agent_opening"] == "Reloaded opening line."
