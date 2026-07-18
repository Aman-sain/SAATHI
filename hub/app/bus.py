"""In-process async pub/sub (§6.2). Topics are the domain-event vocabulary."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, AsyncIterator

log = logging.getLogger("bus")

TOPICS = (
    "node.telemetry",
    "node.event",
    "vision.event",
    "vision.keypoints",
    "asr.event",
    "alert.created",
    "alert.updated",
    "speak.request",
    "camera.wake",
    "system.health",
)


class EventBus:
    """Bounded fan-out queues; a slow subscriber can never block the fusion engine:
    on overflow the OLDEST item is dropped (§6.2, queues capped at 100).

    Loop-affine: call publish/subscribe only from the event-loop thread — other
    threads (MQTT, DB writer) must hand off via loop.call_soon_threadsafe.
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._maxsize = maxsize
        self._queues: dict[str, list[asyncio.Queue[Any]]] = defaultdict(list)

    def publish(self, topic: str, payload: Any) -> None:
        if topic not in TOPICS:
            log.warning("publish to unknown topic=%s", topic)
        log.info("bus %s %s", topic, _summary(payload))
        for q in self._queues.get(topic, ()):
            if q.full():
                q.get_nowait()
                log.warning("bus %s slow subscriber, dropped oldest", topic)
            q.put_nowait(payload)

    def subscribe(self, topic: str) -> AsyncIterator[Any]:
        # register eagerly, NOT inside the generator: events published between
        # subscribe() and the first `async for` iteration must still be buffered
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._maxsize)
        self._queues[topic].append(q)

        async def _stream() -> AsyncIterator[Any]:
            try:
                while True:
                    yield await q.get()
            finally:
                self._queues[topic].remove(q)

        return _stream()


def _summary(payload: Any, limit: int = 120) -> str:
    """§17: one-line payload summary — INFO logs stay one line per bus event."""
    text = repr(payload)
    return text if len(text) <= limit else text[: limit - 1] + "…"
