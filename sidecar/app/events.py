"""Event bus: SIP callbacks fire on pjsua2's own threads, subscribers live in asyncio.

Everything the sidecar knows about a call reaches Home Assistant through here, so
the bus is deliberately dumb: fan out to every subscriber, never block a publisher,
and keep a short history so a client that reconnects can see what it missed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Call lifecycle, in the order a healthy page goes through them.
CALLING = "calling"
EARLY = "early"
CONFIRMED = "confirmed"
PLAYBACK_STARTED = "playback_started"
PLAYBACK_FINISHED = "playback_finished"
PLAYBACK_PAUSED = "playback_paused"
PLAYBACK_RESUMED = "playback_resumed"
DISCONNECTED = "disconnected"

# Account lifecycle.
REGISTERED = "registered"
UNREGISTERED = "unregistered"


class EventBus:
    def __init__(self, loop: asyncio.AbstractEventLoop, history: int = 100) -> None:
        self._loop = loop
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=history)
        self._seq = 0

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def recent(self, limit: int = 20) -> list[dict]:
        return list(self._history)[-limit:]

    def publish(self, event_type: str, **fields: Any) -> dict:
        """Publish from the asyncio thread."""
        self._seq += 1
        event = {"seq": self._seq, "type": event_type, "ts": time.time(), **fields}
        self._history.append(event)
        _LOGGER.info("event %s %s", event_type, {k: v for k, v in fields.items() if k != "ts"})
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client must never wall up the SIP stack. Drop it and
                # let it resubscribe; /events/recent lets it catch up.
                _LOGGER.warning("dropping a slow event subscriber")
                self._subscribers.discard(queue)
        return event

    def publish_threadsafe(self, event_type: str, **fields: Any) -> None:
        """Publish from a pjsua2 callback thread."""
        self._loop.call_soon_threadsafe(lambda: self.publish(event_type, **fields))
