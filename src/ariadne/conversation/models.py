"""Typed records for Ariadne's one continuous conversation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

Speaker = Literal["human", "iris"]
MessageOrigin = Literal["direct", "proactive"]
MessageKind = Literal["text", "voice", "question", "answer"]
AttachmentKind = Literal["image", "document", "file"]
SpeechPhase = Literal["commentary", "final"]
NotifyLevel = Literal["passive", "active", "time_sensitive"]
TurnKind = Literal["direct", "proactive"]
TurnState = Literal["running", "waiting_input", "stopping"]
TurnOutcome = Literal["completed", "silent", "stopped", "failed"]
InputDisposition = Literal["started", "steered", "queued", "answered", "duplicate"]

SPEAKERS: tuple[Speaker, ...] = ("human", "iris")
ORIGINS: tuple[MessageOrigin, ...] = ("direct", "proactive")


@dataclass(frozen=True, slots=True)
class Attachment:
    """One private file attached to a conversation message."""

    id: str
    kind: AttachmentKind
    filename: str
    mime_type: str | None
    size_bytes: int
    path: Path


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """A message ready to commit; the log assigns its durable identity."""

    speaker: Speaker
    origin: MessageOrigin
    kind: MessageKind
    text: str
    sent_at: datetime
    client_message_id: str | None = None
    reply_to_id: int | None = None
    turn_id: str | None = None
    phase: SpeechPhase | None = None
    stopped: bool = False
    question_id: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    """One permanent message in the private conversation."""

    id: int
    speaker: Speaker
    origin: MessageOrigin
    kind: MessageKind
    text: str
    sent_at: datetime
    client_message_id: str | None = None
    reply_to_id: int | None = None
    turn_id: str | None = None
    phase: SpeechPhase | None = None
    stopped: bool = False
    question_id: str | None = None
    attachments: tuple[Attachment, ...] = ()
    reactions: tuple[str, ...] = ()

    def history_payload(self) -> dict[str, object]:
        """Return the bounded view Iris receives when reading recent messages."""
        payload: dict[str, object] = {
            "message_id": self.id,
            "sent_at": self.sent_at.isoformat(),
            "speaker": self.speaker,
            "origin": self.origin,
            "kind": self.kind,
            "text": self.text,
        }
        if self.reply_to_id is not None:
            payload["reply_to_message_id"] = self.reply_to_id
        if self.attachments:
            payload["attachments"] = [
                {"kind": item.kind, "filename": item.filename}
                for item in self.attachments
            ]
        if self.reactions:
            payload["reactions"] = list(self.reactions)
        if self.stopped:
            payload["stopped"] = True
        return payload


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """A bounded chronological selection of conversation messages."""

    messages: tuple[ConversationMessage, ...]
    total: int
    earliest_available_at: datetime | None

    @property
    def truncated(self) -> bool:
        return self.total > len(self.messages)


@dataclass(frozen=True, slots=True)
class PendingInput:
    """Accepted human input that has not yet reached a Codex turn."""

    id: int
    message_id: int
    prompt: str
    image_paths: tuple[Path, ...]
    force_next_turn: bool
