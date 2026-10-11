"""Fan committed and live conversation events out to connected clients."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from .events import DURABLE_EVENTS, StreamEvent
from .journal import EventJournal

LOGGER = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 2_000


class Subscription:
    """One client's ordered view of published events."""

    def __init__(self, bus: EventBus, *, queue_size: int) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(queue_size)
        self.overflowed = False

    def offer(self, payload: dict[str, Any]) -> None:
        if self.overflowed:
            return
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # A client that cannot keep up must reconnect and resume from its
            # cursor; silently dropping events would corrupt its view.
            self.overflowed = True
            LOGGER.warning("Conversation stream subscriber overflowed")

    async def next(self) -> dict[str, Any] | None:
        """Return the next event, or None once the subscriber has overflowed."""
        if self.overflowed:
            return None
        payload = await self._queue.get()
        return None if self.overflowed else payload

    def close(self) -> None:
        self._bus.unsubscribe(self)


class EventBus:
    """Journal durable events, then broadcast every event in publish order."""

    def __init__(
        self,
        journal: EventJournal,
        *,
        boot_id: str | None = None,
        queue_size: int = SUBSCRIBER_QUEUE_SIZE,
    ) -> None:
        self.journal = journal
        self.boot_id = boot_id or secrets.token_urlsafe(8)
        self._queue_size = queue_size
        self._subscribers: set[Subscription] = set()

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    def latest_seq(self) -> int:
        return self.journal.latest()

    def publish(self, event: StreamEvent) -> dict[str, Any]:
        payload = event.model_dump(mode="json", exclude={"seq", "boot_id"})
        seq = (
            self.journal.append(payload) if isinstance(event, DURABLE_EVENTS) else None
        )
        published = {**payload, "seq": seq, "boot_id": self.boot_id}
        for subscription in tuple(self._subscribers):
            subscription.offer(published)
        return published

    def replay(self, after: int) -> tuple[dict[str, Any], ...]:
        """Return committed events after a client's cursor in publish order."""
        events: list[dict[str, Any]] = []
        cursor = after
        while batch := self.journal.after(cursor):
            events.extend(
                {**entry.payload, "seq": entry.seq, "boot_id": self.boot_id}
                for entry in batch
            )
            cursor = batch[-1].seq
        return tuple(events)

    def subscribe(self) -> Subscription:
        subscription = Subscription(self, queue_size=self._queue_size)
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)
