"""
transports.py — Transport adapters for CascadeSession (cascade.py).

Both classes implement the duck-type CascadeSession expects:
  async send_audio(chunk: bytes)   # forward one chunk of agent audio out
  async send_event(event: dict)    # forward a JSON protocol event out
  async clear_audio()              # tell the far end to stop/flush playback now

BrowserTransport wraps a FastAPI WebSocket for the browser call UI protocol
(binary frames = PCM audio, JSON text frames = events).

TwilioTransport wraps a FastAPI WebSocket carrying Twilio Media Streams
JSON frames (mulaw audio base64-encoded inside {"event":"media",...}).
"""

import asyncio
import base64
import logging
import time

from fastapi import WebSocket

import event_bus

logger = logging.getLogger("voxniac_one.transports")


class BrowserTransport:
    """Browser call UI transport: binary WS frames for audio, JSON for events."""

    def __init__(self, ws: WebSocket):
        self.ws = ws

    async def send_audio(self, chunk: bytes):
        await self.ws.send_bytes(chunk)

    async def send_event(self, event: dict):
        await self.ws.send_json(event)

    async def clear_audio(self):
        # The browser client stops all scheduled playback on the "barge_in"
        # event itself (see the WS protocol); nothing extra to do server-side.
        return


class TwilioTransport:
    """Twilio Media Streams transport: JSON-framed base64 mulaw audio.

    Twilio expects mulaw @ 8kHz paced at real time (20ms frames of 160 bytes).
    Sending a whole TTS chunk in one burst outruns Twilio's playback buffer and
    causes audible distortion, especially right after the call connects. To
    avoid that, send_audio() slices each chunk into 160-byte (20ms) frames and
    paces them against a monotonic "playhead": frames are allowed to run up to
    LEAD_S ahead of real time (so short chunks still leave with low latency),
    and any further frames are held with asyncio.sleep() until real time
    catches up to (playhead - LEAD_S).
    """

    FRAME_BYTES = 160  # 20ms of 8kHz mulaw (1 byte/sample)
    FRAME_DURATION_S = 0.02
    LEAD_S = 0.4  # how far ahead of real time we're allowed to send

    def __init__(self, ws: WebSocket, call_id: "str | None" = None):
        self.ws = ws
        self.stream_sid = None
        # Phase 3.5 P1: set once the call_id is known (server.py assigns it
        # right after the Twilio "start" event arrives, same as stream_sid) —
        # used to tag every event fanned out to event_bus below.
        self.call_id = call_id
        # Monotonic timestamp marking the scheduled real-time playback point of
        # the next frame to be sent. None means "no pacing reference yet" (i.e.
        # freshly started or just cleared) -> the next send_audio() call resyncs
        # it to "now", regranting a full LEAD_S of burst allowance.
        self._playhead = None

    async def send_audio(self, chunk: bytes):
        if not self.stream_sid:
            logger.warning("twilio_out: dropping audio chunk, no streamSid yet")
            return
        now = time.monotonic()
        if self._playhead is None or self._playhead < now - self.LEAD_S:
            # First chunk ever, or the playhead fell behind real time (e.g. a
            # gap since the last chunk already finished playing out) -> resync
            # so we don't try to catch up by bursting the new chunk instead of
            # pacing it.
            self._playhead = now
        for offset in range(0, len(chunk), self.FRAME_BYTES):
            frame = chunk[offset:offset + self.FRAME_BYTES]
            if not frame:
                continue
            now = time.monotonic()
            sleep_for = self._playhead - now - self.LEAD_S
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            payload = base64.b64encode(frame).decode("ascii")
            await self.ws.send_json({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload},
            })
            self._playhead += self.FRAME_DURATION_S

    async def send_event(self, event: dict):
        # Twilio Media Streams has no generic JSON event channel to the caller;
        # only "clear"/"mark" are meaningful protocol messages. Translate what
        # we can (barge_in -> clear) — Twilio itself never sees the rest.
        if event.get("type") == "barge_in":
            await self.clear_audio()
        # Phase 3.5 P1: fan every event out to any subscribed UI monitor
        # (GET/WS /ws/monitor) so a live phone call's transcript is visible in
        # the browser, same as the browser call's own protocol. Fire-and-
        # forget — event_bus.publish() never awaits network I/O, so this can
        # never add latency to the audio pipeline.
        event_bus.publish("twilio", self.call_id, event)

    def clear(self):
        """Resets the pacing playhead. Called from clear_audio() so a barge-in
        doesn't leave stale pacing state that would make the next utterance's
        audio think it's still behind schedule (and burst to "catch up")."""
        self._playhead = None

    async def clear_audio(self):
        self.clear()
        if not self.stream_sid:
            return
        await self.ws.send_json({"event": "clear", "streamSid": self.stream_sid})


def parse_twilio_event(raw: dict):
    """
    Parses one Twilio Media Streams JSON frame into (kind, value):
      ("connected", None)
      ("start", {"stream_sid": str | None, "custom_parameters": dict})
      ("media", audio_bytes: bytes)   # decoded mulaw payload
      ("stop", None)
      ("unknown", raw)

    Phase 3.5 P1: "start"'s value is now a small dict instead of a bare
    stream_sid string, so callers can also read custom_parameters — in
    particular the "to" phone number, which call_launcher.py now passes via
    a TwiML <Parameter name="to" value="+1..."/> inside <Stream> (Twilio
    Media Streams has no other way to tell /ws/twilio which number is on the
    line). Used by server.py to build a call_id like "20260709_143207_0100".
    """
    event = raw.get("event")
    if event == "connected":
        return "connected", None
    if event == "start":
        start_obj = raw.get("start") or {}
        stream_sid = start_obj.get("streamSid") or raw.get("streamSid")
        custom_parameters = start_obj.get("customParameters")
        if not isinstance(custom_parameters, dict):
            custom_parameters = {}
        return "start", {"stream_sid": stream_sid, "custom_parameters": custom_parameters}
    if event == "media":
        payload_b64 = (raw.get("media") or {}).get("payload")
        if not payload_b64:
            return "media", b""
        try:
            audio = base64.b64decode(payload_b64)
        except (ValueError, TypeError) as exc:
            logger.error("twilio_in: failed to decode media payload: %s", exc)
            return "media", b""
        return "media", audio
    if event == "stop":
        return "stop", None
    return "unknown", raw
