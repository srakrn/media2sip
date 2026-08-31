"""The event bus."""

from __future__ import annotations

import asyncio

import pytest

from app.events import EventBus


async def test_subscribers_receive_events() -> None:
    bus = EventBus(asyncio.get_running_loop())
    queue = bus.subscribe()

    bus.publish("calling", call_id="abc", target="991")

    event = await asyncio.wait_for(queue.get(), 1)
    assert event["type"] == "calling"
    assert event["call_id"] == "abc"
    assert event["seq"] == 1


async def test_history_lets_a_reconnecting_client_catch_up() -> None:
    bus = EventBus(asyncio.get_running_loop(), history=3)
    for i in range(5):
        bus.publish("calling", call_id=str(i))

    recent = bus.recent(10)
    assert [e["call_id"] for e in recent] == ["2", "3", "4"]


async def test_a_stalled_subscriber_is_dropped_not_tolerated() -> None:
    """A stalled client must never wall up the SIP stack."""
    bus = EventBus(asyncio.get_running_loop())
    stalled = bus.subscribe()
    healthy = bus.subscribe()

    for i in range(stalled.maxsize + 5):
        bus.publish("calling", call_id=str(i))
        # The healthy subscriber keeps up; the stalled one never reads.
        healthy.get_nowait()

    assert stalled not in bus._subscribers
    assert healthy in bus._subscribers

    # And the bus keeps serving the survivor.
    bus.publish("confirmed", call_id="after")
    assert healthy.get_nowait()["call_id"] == "after"


async def test_unsubscribe() -> None:
    bus = EventBus(asyncio.get_running_loop())
    queue = bus.subscribe()
    bus.unsubscribe(queue)
    bus.publish("calling", call_id="abc")
    assert queue.empty()


async def test_publish_threadsafe_reaches_the_loop() -> None:
    """SIP callbacks fire on pjsua2's thread, subscribers live in asyncio."""
    import threading

    bus = EventBus(asyncio.get_running_loop())
    queue = bus.subscribe()

    threading.Thread(
        target=lambda: bus.publish_threadsafe("confirmed", call_id="xyz")
    ).start()

    event = await asyncio.wait_for(queue.get(), 2)
    assert event["type"] == "confirmed"
    assert event["call_id"] == "xyz"
