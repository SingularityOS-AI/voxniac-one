"""Tests that CascadeSession accepts call_id/channel and passes them through
to log_turn() (Phase 3.5 P1). Runs a full _run_turn() with the LLM (stream_
chat) and the REST TTS fallback (sintetizar) monkeypatched to synchronous
fakes — no network, no real Deepgram/Fireworks sockets. LiveTTS/LiveSTT are
never .connect()'d (only their constructors run, which touch no network), so
_speak_sentence falls back to the REST path we've faked out.
"""

import io
import wave

import cascade


class FakeTransport:
    """Duck-type: async send_audio(bytes), async send_event(dict), async
    clear_audio(). Records every event/audio chunk seen."""

    def __init__(self):
        self.events = []
        self.audio_chunks = []

    async def send_audio(self, chunk):
        self.audio_chunks.append(chunk)

    async def send_event(self, event):
        self.events.append(event)

    async def clear_audio(self):
        pass


def _tiny_wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x00\x00" * 100)
    return buf.getvalue()


def _fake_stream_chat(model_id, reasoning_effort, history, on_token=None, stop_event=None, **kwargs):
    if on_token:
        on_token("Hello there.")
    return "Hello there.", 0.01, 0.02


def _fake_sintetizar(engine, text):
    return _tiny_wav_bytes(), 0.01


async def test_run_turn_passes_call_id_and_channel_to_log_turn(monkeypatch):
    monkeypatch.setattr(cascade, "stream_chat", _fake_stream_chat)
    monkeypatch.setattr(cascade, "sintetizar", _fake_sintetizar)

    logged = []
    monkeypatch.setattr(cascade, "log_turn", lambda entry: logged.append(entry))

    transport = FakeTransport()
    session = cascade.CascadeSession(
        transport=transport,
        stt_cfg={"encoding": "mulaw", "sample_rate": 8000},
        llm_cfg={"model_id": "accounts/fireworks/models/kimi-k2p6"},
        tts_cfg={"encoding": "linear16", "sample_rate": 24000},
        profile={"llm_model": "accounts/fireworks/models/kimi-k2p6"},
        call_id="20260709_143207_0100",
        channel="twilio",
    )
    # Never connected live STT/TTS sockets (start() was never called) -> the
    # REST TTS fallback path is what actually runs.
    session._tts_live_available = False

    await session._run_turn("Hi, who is this?", 100.0, 100.5)

    assert len(logged) == 1
    entry = logged[0]
    assert entry["call_id"] == "20260709_143207_0100"
    assert entry["channel"] == "twilio"
    assert entry["user_text"] == "Hi, who is this?"
    assert entry["agent_text"] == "Hello there."

    # Sanity: the fake agent reply actually made it out to the transport too.
    done_events = [e for e in transport.events if e.get("type") == "agent_done"]
    assert done_events and done_events[0]["text"] == "Hello there."


async def test_run_turn_defaults_call_id_and_channel_when_not_provided(monkeypatch):
    """Backward compatibility: a caller that doesn't pass call_id/channel
    (defaults) must not crash and must still log something sensible."""
    monkeypatch.setattr(cascade, "stream_chat", _fake_stream_chat)
    monkeypatch.setattr(cascade, "sintetizar", _fake_sintetizar)

    logged = []
    monkeypatch.setattr(cascade, "log_turn", lambda entry: logged.append(entry))

    transport = FakeTransport()
    session = cascade.CascadeSession(
        transport=transport,
        stt_cfg={"encoding": "linear16", "sample_rate": 16000},
        llm_cfg={"model_id": "accounts/fireworks/models/kimi-k2p6"},
        tts_cfg={"encoding": "linear16", "sample_rate": 24000},
        profile={"llm_model": "accounts/fireworks/models/kimi-k2p6"},
    )
    session._tts_live_available = False

    await session._run_turn("hello", 100.0, 100.2)

    assert logged[0]["call_id"] is None
    assert logged[0]["channel"] == "unknown"
