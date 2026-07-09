"""
event_bus.py — Phase 3.5 P1: fire-and-forget fan-out bus for live call
monitoring.

Twilio phone calls have no client-side WebSocket of their own to render
events on — TwilioTransport.send_event() only understands "barge_in"
(-> Twilio "clear"); every other cascade.py event (stt_partial, stt_final,
agent_token, agent_done, metrics, error...) previously had nowhere to go.

This module lets any number of UI "monitor" connections (server.py's
GET/WS /ws/monitor) observe those same events live, each wrapped with
{"channel": "twilio", "call_id": "...", "event": {...}} so the browser can
tell which call — and which channel — an event belongs to.

Latency-safe by construction: publish() is a plain synchronous function.
It never awaits network I/O itself (that happens independently in each
monitor connection's own forwarding loop in server.py) and never blocks —
a full subscriber queue just drops its own oldest pending item instead of
back-pressuring the caller. This guarantees the Twilio audio pipeline
(cascade.py / transports.py) can call publish() inline without adding any
latency to a live call, satisfying PLAN_FASE3_5.md's "fan-out is
fire-and-forget" house rule.
"""

import asyncio
import logging

logger = logging.getLogger("voxniac_one.event_bus")

# Bounded per-subscriber queue: a slow/stuck UI monitor can never grow memory
# unboundedly, and never blocks other subscribers or the publisher.
_MAX_QUEUE = 200

_subscribers: "set[asyncio.Queue]" = set()


def subscribe() -> asyncio.Queue:
    """Registers a new monitor subscriber and returns its queue. Must be
    called from within a running asyncio event loop (asyncio.Queue is
    loop-bound)."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue):
    """Removes a subscriber (e.g. when its WebSocket disconnects). Safe to
    call even if the queue was never subscribed or already removed."""
    _subscribers.discard(queue)


def subscriber_count() -> int:
    return len(_subscribers)


def publish(channel: str, call_id: "str | None", event: dict):
    """
    Fire-and-forget: wraps `event` with channel/call_id context and enqueues
    it onto every currently-subscribed monitor's queue. Never raises, never
    awaits. If nobody is subscribed, this is a no-op (the common case for
    most of a call's lifetime when no monitor tab is open).
    """
    if not _subscribers:
        return
    envelope = {"channel": channel, "call_id": call_id, "event": event}
    for queue in list(_subscribers):
        try:
            queue.put_nowait(envelope)
            continue
        except asyncio.QueueFull:
            pass
        # Queue is full (a monitor that isn't draining fast enough): drop the
        # oldest pending item for THIS subscriber only and retry once. Never
        # blocks, never affects other subscribers or the caller.
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(envelope)
        except asyncio.QueueFull:
            logger.warning("event_bus: subscriber queue still full after dropping the oldest item, skipping")
