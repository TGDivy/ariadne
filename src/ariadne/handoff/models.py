"""Typed records for durable background-to-conversation handoffs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

HandoffStatus = Literal["staged", "ready", "claimed", "completed", "discarded"]


@dataclass(frozen=True, slots=True)
class ConversationHandoff:
    """One free-form update owned by a single background activation."""

    id: str
    activation_key: str
    source: str
    body: str
    status: HandoffStatus
    error: str | None
    created_at: datetime
    updated_at: datetime
    released_at: datetime | None
    claimed_at: datetime | None
    completed_at: datetime | None

    def context_payload(self) -> dict[str, str]:
        """Return the bounded context useful to the shared conversation."""
        return {
            "id": self.id,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "text": self.body,
        }
