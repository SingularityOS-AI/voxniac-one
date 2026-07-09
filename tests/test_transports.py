"""Tests for transports.py's Phase 3.5 P1 changes: parse_twilio_event()'s new
"start" shape (custom_parameters) and TwilioTransport.send_event() fanning
out to event_bus. No network — uses a FakeWebSocket, never a real one."""

import base64

import event_bus
import transports


class FakeWebSocket:
    """Minimal duck-type: only what TwilioTransport actually calls."""

    def __init__(self):
        self.sent_json = []

    async def send_json(self, data):
        self.sent_json.append(data)


# ---------------------------------------------------------------------------
# parse_twilio_event
# ---------------------------------------------------------------------------
def test_parse_connected():
    kind, value = transports.parse_twilio_event({"event": "connected"})
    assert kind == "connected"
    assert value is None


def test_parse_start_with_custom_parameters():
    raw = {
        "event": "start",
        "start": {
            "streamSid": "MZ123",
            "callSid": "CA123",
            "customParameters": {"to": "+13075550100"},
        },
        "streamSid": "MZ123",
    }
    kind, value = transports.parse_twilio_event(raw)
    assert kind == "start"
    assert value["stream_sid"] == "MZ123"
    assert value["custom_parameters"] == {"to": "+13075550100"}


def test_parse_start_without_custom_parameters_is_bulletproof():
    raw = {"event": "start", "start": {"streamSid": "MZ999"}}
    kind, value = transports.parse_twilio_event(raw)
    assert kind == "start"
    assert value["stream_sid"] == "MZ999"
    assert value["custom_parameters"] == {}


def test_parse_media_decodes_base64_mulaw():
    payload = base64.b64encode(b"\x00\x01\x02").decode()
    kind, value = transports.parse_twilio_event({"event": "media", "media": {"payload": payload}})
    assert kind == "media"
    assert value == b"\x00\x01\x02"


def test_parse_stop():
    kind, value = transports.parse_twilio_event({"event": "stop"})
    assert kind == "stop"
    assert value is None


def test_parse_unknown_event_passthrough():
    raw = {"event": "mark", "mark": {"name": "x"}}
    kind, value = transports.parse_twilio_event(raw)
    assert kind == "unknown"
    assert value == raw


# ---------------------------------------------------------------------------
# TwilioTransport.send_event -> event_bus fan-out
# ---------------------------------------------------------------------------
async def test_twilio_transport_send_event_publishes_to_bus():
    event_bus._subscribers.clear()
    queue = event_bus.subscribe()
    try:
        ws = FakeWebSocket()
        transport = transports.TwilioTransport(ws, call_id="call123")

        await transport.send_event({"type": "stt_final", "text": "hello"})

        envelope = queue.get_nowait()
        assert envelope["channel"] == "twilio"
        assert envelope["call_id"] == "call123"
        assert envelope["event"] == {"type": "stt_final", "text": "hello"}
    finally:
        event_bus.unsubscribe(queue)
        event_bus._subscribers.clear()


async def test_twilio_transport_barge_in_still_clears_and_publishes():
    event_bus._subscribers.clear()
    queue = event_bus.subscribe()
    try:
        ws = FakeWebSocket()
        transport = transports.TwilioTransport(ws, call_id="call456")
        transport.stream_sid = "MZ1"

        await transport.send_event({"type": "barge_in"})

        # Twilio-facing "clear" frame still gets sent (existing behavior).
        assert any(msg.get("event") == "clear" for msg in ws.sent_json)
        # AND it's still fanned out to the monitor bus (new behavior).
        envelope = queue.get_nowait()
        assert envelope["event"] == {"type": "barge_in"}
    finally:
        event_bus.unsubscribe(queue)
        event_bus._subscribers.clear()
