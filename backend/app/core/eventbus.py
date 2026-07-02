"""In-process pub/sub event bus.

Single-process architecture: services publish domain events here; the FastAPI
websocket layer and other services subscribe. Subscribers get their own
bounded queue so one slow consumer (e.g. a dashboard tab on bad wifi) never
back-pressures the trading path — on overflow the oldest event is dropped for
that subscriber only.
"""
import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(slots=True)
class Event:
    topic: str
    payload: dict[str, Any]
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "payload": self.payload, "ts": self.ts.isoformat()}


class EventBus:
    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def publish(self, topic: str, payload: dict[str, Any]) -> Event:
        event = Event(topic=topic, payload=payload)
        for queue in self._subscribers:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)
        return event

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(self._queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


bus = EventBus()
