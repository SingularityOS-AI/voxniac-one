"""Tests for event_bus.py (Phase 3.5 P1 fan-out bus). No network."""

import pytest

import event_bus


@pytest.fixture(autouse=True)
def _clean_subscribers():
    """event_bus keeps a module-level set of subscribers; make sure tests
    never leak queues into each other."""
    event_bus._subscribers.clear()
    yield
    event_bus._subscribers.clear()


async def test_publish_with_no_subscribers_is_a_noop():
    # Must not raise even though nobody is listening.
    event_bus.publish("twilio", "call1", {"type": "stt_final", "text": "hi"})
    assert event_bus.subscriber_count() == 0


async def test_subscribe_publish_unsubscribe_round_trip():
    queue = event_bus.subscribe()
    assert event_bus.subscriber_count() == 1

    event_bus.publish("twilio", "call1", {"type": "agent_done", "text": "hello"})

    envelope = queue.get_nowait()
    assert envelope == {
        "channel": "twilio",
        "call_id": "call1",
        "event": {"type": "agent_done", "text": "hello"},
    }

    event_bus.unsubscribe(queue)
    assert event_bus.subscriber_count() == 0

    # unsubscribe is idempotent / safe to call twice.
    event_bus.unsubscribe(queue)


async def test_publish_fans_out_to_every_subscriber():
    q1 = event_bus.subscribe()
    q2 = event_bus.subscribe()

    event_bus.publish("twilio", "callX", {"type": "metrics", "e2e_s": 1.2})

    env1 = q1.get_nowait()
    env2 = q2.get_nowait()
    assert env1["call_id"] == "callX"
    assert env2["call_id"] == "callX"
    assert env1["event"]["type"] == "metrics"
    assert env2["event"]["type"] == "metrics"


async def test_publish_drops_oldest_when_a_subscriber_queue_is_full():
    queue = event_bus.subscribe()
    # Fill the queue to its cap.
    for i in range(event_bus._MAX_QUEUE):
        event_bus.publish("twilio", "callFull", {"type": "agent_token", "token": str(i)})

    assert queue.full()

    # One more publish must not raise, and must drop the oldest item instead
    # of blocking or crashing the caller.
    event_bus.publish("twilio", "callFull", {"type": "agent_token", "token": "overflow"})

    assert queue.qsize() == event_bus._MAX_QUEUE
    # The oldest item (token "0") should have been dropped.
    first = queue.get_nowait()
    assert first["event"]["token"] != "0"
