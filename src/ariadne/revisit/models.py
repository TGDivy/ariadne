"""Typed records and model choices for one-off future revisits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from openai_codex.generated.v2_all import ReasoningEffort

from ..codex.models import CodexTurnSettings

TOOLS = (
    "schedule_wakeup",
    "list_wakeups",
    "update_wakeup",
    "cancel_wakeup",
)


class Attention(StrEnum):
    """The amount of future computational attention Iris deliberately selects."""

    light = "light"
    focused = "focused"
    deep = "deep"


ATTENTION_SETTINGS: dict[Attention, CodexTurnSettings] = {
    Attention.light: CodexTurnSettings(
        model="gpt-5.6-luna",
        effort=ReasoningEffort.low,
        web_search="live",
    ),
    Attention.focused: CodexTurnSettings(
        model="gpt-5.6-luna",
        effort=ReasoningEffort.high,
        web_search="live",
    ),
    Attention.deep: CodexTurnSettings(
        model="gpt-5.6-terra",
        effort=ReasoningEffort.medium,
        web_search="live",
    ),
}

RevisitStatus = Literal["pending", "running", "completed", "failed"]


def settings_for_attention(attention: Attention) -> CodexTurnSettings:
    """Resolve every declared attention level without a default model path."""
    return ATTENTION_SETTINGS[attention]


def parse_due_at(value: str) -> datetime:
    """Parse one timezone-aware ISO 8601 timestamp and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Revisit time must be a valid ISO 8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Revisit time must include an explicit timezone offset.")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Revisit:
    """One durable future-self note and its operational lifecycle."""

    id: str
    due_at: datetime
    note: str
    attention: Attention
    status: RevisitStatus
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def public_payload(self) -> dict[str, object]:
        """Return only the semantic information useful to Iris."""
        payload: dict[str, object] = {
            "id": self.id,
            "at": self.due_at.isoformat(),
            "note": self.note,
            "attention": self.attention.value,
            "status": self.status,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload
