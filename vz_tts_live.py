"""
vz_tts_live.py — Async Deepgram Aura-2 live WebSocket TTS client.

Sends text (per sentence) to Deepgram's real-time speech-synthesis API as
{"type":"Speak","text":...} followed by {"type":"Flush"}, and receives raw
audio chunks back as binary WebSocket frames. This is the TTS stage of the
ASR -> LLM -> TTS pipeline driven by cascade.py.

Fails loud: connection and protocol errors raise LiveTTSError with a "tts_*"
stage prefix. The caller (cascade.py) decides whether to fall back to the REST
engine in vz_tts.py, per the "only fall back on live-WS connection failure"
principle.

See vz_stt_live.py's module docstring for the `additional_headers` vs
`extra_headers` websockets-version note (identical situation here).
"""

import asyncio
import json
import logging

import websockets

from vz_config import DEEPGRAM_API_KEY

logger = logging.getLogger("voxniac_one.tts_live")

DEEPGRAM_TTS_URL = "wss://api.deepgram.com/v1/speak"


class LiveTTSError(RuntimeError):
    """Raised on any Deepgram live TTS connection or protocol failure."""


class LiveTTS:
    """Async Deepgram Aura-2 live WebSocket TTS client.

    Usage:
        tts = LiveTTS(on_flushed=..., on_error=...)
        await tts.connect(encoding="linear16", sample_rate=24000, voice="aura-2-thalia-en")
        await tts.speak("Hello there.")   # Speak + Flush
        chunk = await tts.audio_queue.get()  # raw audio bytes, in order
        ...
        await tts.clear()   # barge-in: stop synthesizing/streaming immediately
        await tts.close()

    Audio chunks are delivered via `self.audio_queue` (asyncio.Queue of bytes),
    decoupling network receipt from whatever consumes/forwards the audio.
    """

    def __init__(self, on_flushed=None, on_cleared=None, on_error=None):
        self.on_flushed = on_flushed
        self.on_cleared = on_cleared
        self.on_error = on_error
        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self._ws = None
        self._recv_task = None
        self._closed = True

    async def connect(self, encoding: str, sample_rate: int, voice: str, container: str = "none"):
        """Opens the Deepgram live TTS WebSocket for the given voice/audio format."""
        params = f"model={voice}&encoding={encoding}&sample_rate={sample_rate}&container={container}"
        url = f"{DEEPGRAM_TTS_URL}?{params}"
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                ping_interval=20,
                ping_timeout=20,
            )
        except Exception as exc:
            raise LiveTTSError(f"tts_connect: Deepgram live TTS connection failed: {exc}") from exc

        self._closed = False
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("Deepgram live TTS connected (voice=%s, encoding=%s, sample_rate=%s)", voice, encoding, sample_rate)

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                self._handle_message(raw)
        except websockets.exceptions.ConnectionClosed as exc:
            if not self._closed:
                logger.error("tts_recv: Deepgram live TTS connection closed unexpectedly: %s", exc)
                if self.on_error:
                    self.on_error(LiveTTSError(f"tts_recv: connection closed unexpectedly: {exc}"))
        except Exception as exc:
            logger.error("tts_recv: Deepgram live TTS receive loop failed: %s", exc)
            if self.on_error:
                self.on_error(LiveTTSError(f"tts_recv: {exc}"))

    def _handle_message(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            self.audio_queue.put_nowait(bytes(raw))
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type")
        if msg_type == "Flushed":
            if self.on_flushed:
                self.on_flushed()
        elif msg_type == "Cleared":
            if self.on_cleared:
                self.on_cleared()
        elif msg_type == "Warning":
            logger.warning("Deepgram live TTS warning: %s", data)
        elif msg_type == "Error":
            logger.error("Deepgram live TTS reported a provider error: %s", data)
            if self.on_error:
                self.on_error(LiveTTSError(f"tts_provider: {data}"))
        # Metadata: informational only, no action needed.

    async def speak(self, text: str):
        """Sends text to be synthesized immediately: {"type":"Speak"} + {"type":"Flush"}."""
        if self._ws is None or self._closed:
            raise LiveTTSError("tts_speak: attempted to speak on a closed/unconnected TTS socket")
        try:
            await self._ws.send(json.dumps({"type": "Speak", "text": text}))
            await self._ws.send(json.dumps({"type": "Flush"}))
        except Exception as exc:
            raise LiveTTSError(f"tts_speak: Deepgram live TTS send failed: {exc}") from exc

    async def clear(self):
        """Barge-in: tells Deepgram to stop synthesizing/streaming the current turn's audio."""
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({"type": "Clear"}))
        except Exception as exc:
            logger.error("tts_clear: Deepgram live TTS clear failed: %s", exc)
        # Drop anything already queued locally that hasn't been forwarded yet.
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def close(self):
        """Closes the Deepgram live TTS connection and stops the receive loop."""
        if self._closed:
            return
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "Close"}))
            except Exception:
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("Deepgram live TTS closed")
