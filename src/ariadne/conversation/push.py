"""The boundary through which conversation messages reach a locked phone."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import NotifyLevel

MAX_PUSH_BODY_LENGTH = 180


@dataclass(frozen=True, slots=True)
class PushNotification:
    """One alert for a message the owner has not seen in an open app."""

    title: str
    body: str
    level: NotifyLevel
    message_id: int


class PushGateway(Protocol):
    async def notify(self, notification: PushNotification) -> None: ...


class NullPushGateway:
    """Deliver nothing; used until a push provider is configured."""

    async def notify(self, notification: PushNotification) -> None:
        return None


class RecordingPushGateway:
    """Keep every notification in memory for the dev server and tests."""

    def __init__(self) -> None:
        self.notifications: list[PushNotification] = []

    async def notify(self, notification: PushNotification) -> None:
        self.notifications.append(notification)


def push_body(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_PUSH_BODY_LENGTH:
        return collapsed
    return f"{collapsed[: MAX_PUSH_BODY_LENGTH - 1].rstrip()}…"
