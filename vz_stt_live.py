"""
vz_stt_live.py — Async Deepgram nova-3 live WebSocket STT client.

Streams raw audio to Deepgram's real-time transcription API and surfaces
partial transcripts, final transcripts, and speech-start events via callbacks.
This is the STT stage of the ASR -> LLM -> TTS pipeline driven by cascade.py.

Fails loud: connection and protocol errors raise LiveSTTError with a "stt_*"
stage prefix. The caller (cascade.py) decides whether to fall back to the batch
engine in vz_asr.py, per the "only fall back on live-WS connection failure"
principle.

Note: uses the `additional_headers` kwarg of websockets.connect(), which
requires websockets>=13 (the currently pinned websockets>=12 minimum still
resolves to a version that has this kwarg on all actively maintained releases;
if an older 12.x without it is ever pinned exactly, connect() will need
`extra_headers` instead).
"""

import asyncio
import json
import logging

import websockets

from vz_config import DEEPGRAM_API_KEY

logger = logging.getLogger("voxniac_one.stt_live")

DEEPGRAM_STT_URL = "wss://api.deepgram.com/v1/listen"
KEEPALIVE_INTERVAL_S = 5.0


class LiveSTTError(RuntimeError):
    """Raised on any Deepgram live STT connection or protocol failure."""


class LiveSTT:
    """Async Deepgram nova-3 live WebSocket STT client.

    Usage:
        stt = LiveSTT(on_partial=..., on_final=..., on_speech_started=..., on_error=...)
        await stt.connect(encoding="linear16", sample_rate=16000)
        await stt.send_audio(pcm_bytes)
        ...
        await stt.finalize()
        await stt.close()

    Callbacks are plain sync callables invoked from the internal receive loop
    (itself an asyncio Task), so they run on the event loop thread — safe to
    call asyncio.create_task(...) from inside them.
    """

    def __init__(self, on_partial=None, on_final=None, on_speech_started=None, on_error=None):
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_speech_started = on_speech_started
        self.on_error = on_error
        self._ws = None
        self._recv_task = None
        self._keepalive_task = None
        self._last_send_ts = 0.0
        self._closed = True

    async def connect(self, encoding: str, sample_rate: int, channels: int = 1):
        """Opens the Deepgram live STT WebSocket for the given audio format."""
        params = (
            f"model=nova-3&encoding={encoding}&sample_rate={sample_rate}"
            f"&channels={channels}&interim_results=true&endpointing=300"
            f"&smart_format=true&language=en&vad_events=true"
        )
        url = f"{DEEPGRAM_STT_URL}?{params}"
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                ping_interval=20,
                ping_timeout=20,
            )
        except Exception as exc:
            raise LiveSTTError(f"stt_connect: Deepgram live STT connection failed: {exc}") from exc

        self._closed = False
        self._last_send_ts = asyncio.get_event_loop().time()
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info("Deepgram live STT connected (encoding=%s, sample_rate=%s)", encoding, sample_rate)

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                self._handle_message(raw)
        except websockets.exceptions.ConnectionClosed as exc:
            if not self._closed:
                logger.error("stt_recv: Deepgram live STT connection closed unexpectedly: %s", exc)
                if self.on_error:
                    self.on_error(LiveSTTError(f"stt_recv: connection closed unexpectedly: {exc}"))
        except Exception as exc:
            logger.error("stt_recv: Deepgram live STT receive loop failed: %s", exc)
            if self.on_error:
                self.on_error(LiveSTTError(f"stt_recv: {exc}"))

    def _handle_message(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            return  # Deepgram STT never sends binary frames; ignore defensively.
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type")
        if msg_type == "Results":
            try:
                alt = data["channel"]["alternatives"][0]
                transcript = alt.get("transcript", "")
            except (KeyError, IndexError):
                return
            if not transcript:
                return
            is_final = bool(data.get("is_final"))
            speech_final = bool(data.get("speech_final"))
            if is_final:
                if self.on_final:
                    self.on_final(transcript, speech_final)
            else:
                if self.on_partial:
                    self.on_partial(transcript)
        elif msg_type == "SpeechStarted":
            if self.on_speech_started:
                self.on_speech_started()
        elif msg_type == "Error":
            logger.error("Deepgram live STT reported a provider error: %s", data)
            if self.on_error:
                self.on_error(LiveSTTError(f"stt_provider: {data}"))
        # UtteranceEnd / Metadata: informational only, no action needed.

    async def _keepalive_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(1.0)
                now = asyncio.get_event_loop().time()
                if now - self._last_send_ts >= KEEPALIVE_INTERVAL_S:
                    try:
                        await self._ws.send(json.dumps({"type": "KeepAlive"}))
                        self._last_send_ts = now
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    async def send_audio(self, chunk: bytes):
        """Sends a raw audio chunk already encoded in the connected format."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(chunk)
            self._last_send_ts = asyncio.get_event_loop().time()
        except Exception as exc:
            raise LiveSTTError(f"stt_send: Deepgram live STT send failed: {exc}") from exc

    async def finalize(self):
        """Asks Deepgram to flush and finalize the current utterance immediately."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({"type": "Finalize"}))
        except Exception as exc:
            logger.error("stt_finalize: Deepgram live STT finalize failed: %s", exc)

    async def close(self):
        """Closes the Deepgram live STT connection and stops background tasks."""
        if self._closed:
            return
        self._closed = True
        for task in (self._keepalive_task, self._recv_task):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("Deepgram live STT closed")
