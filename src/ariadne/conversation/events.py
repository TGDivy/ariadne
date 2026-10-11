"""Events the conversation publishes to connected clients.

Durable events enter the journal and carry a monotonic `seq`, so a client that
reconnects can replay what it missed. Live events (streaming speech, activity,
turn state) carry no `seq`; a reconnecting client rebuilds them from a snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    Attachment,
    AttachmentKind,
    ConversationMessage,
    InputDisposition,
    MessageKind,
    MessageOrigin,
    NotifyLevel,
    Speaker,
    SpeechPhase,
    TurnKind,
    TurnOutcome,
    TurnState,
)
from .questions import AnswerSource, Question, QuestionStatus


class WireModel(BaseModel):
    """A strict, immutable JSON model shared with the app."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AttachmentView(WireModel):
    id: str
    kind: AttachmentKind
    filename: str
    mime_type: str | None
    size_bytes: int

    @classmethod
    def of(cls, attachment: Attachment) -> AttachmentView:
        return cls(
            id=attachment.id,
            kind=attachment.kind,
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
        )


class MessageView(WireModel):
    id: int
    sent_at: datetime
    speaker: Speaker
    origin: MessageOrigin
    kind: MessageKind
    text: str
    client_message_id: str | None = None
    reply_to_id: int | None = None
    turn_id: str | None = None
    phase: SpeechPhase | None = None
    stopped: bool = False
    question_id: str | None = None
    attachments: tuple[AttachmentView, ...] = ()
    reactions: tuple[str, ...] = ()

    @classmethod
    def of(cls, message: ConversationMessage) -> MessageView:
        return cls(
            id=message.id,
            sent_at=message.sent_at,
            speaker=message.speaker,
            origin=message.origin,
            kind=message.kind,
            text=message.text,
            client_message_id=message.client_message_id,
            reply_to_id=message.reply_to_id,
            turn_id=message.turn_id,
            phase=message.phase,
            stopped=message.stopped,
            question_id=message.question_id,
            attachments=tuple(AttachmentView.of(item) for item in message.attachments),
            reactions=message.reactions,
        )


class QuestionView(WireModel):
    id: str
    prompt: str
    choices: tuple[str, ...]
    status: QuestionStatus
    expires_at: datetime
    message_id: int | None = None
    answer: str | None = None
    answer_source: AnswerSource | None = None

    @classmethod
    def of(cls, question: Question) -> QuestionView:
        return cls(
            id=question.question_id,
            prompt=question.prompt,
            choices=question.choices,
            status=question.status,
            expires_at=datetime.fromtimestamp(question.expires_at, UTC),
            message_id=question.message_id,
            answer=question.answer,
            answer_source=question.answer_source,
        )


class SpeechView(WireModel):
    item_id: str
    phase: SpeechPhase
    text: str


class LiveTurnView(WireModel):
    turn_id: str
    kind: TurnKind
    state: TurnState
    activity: str | None = None
    work_summary: str | None = None
    speech: SpeechView | None = None


class StreamEvent(WireModel):
    seq: int | None = None
    boot_id: str = ""


class Snapshot(StreamEvent):
    type: Literal["snapshot"] = "snapshot"
    last_message_id: int | None
    turn: LiveTurnView | None
    question: QuestionView | None
    queued_inputs: int
    reset: bool = Field(
        description="True when missed events are gone; refetch messages from scratch."
    )


class Heartbeat(StreamEvent):
    type: Literal["heartbeat"] = "heartbeat"
    at: datetime


class TurnStarted(StreamEvent):
    type: Literal["turn.started"] = "turn.started"
    turn_id: str
    kind: TurnKind


class TurnStateChanged(StreamEvent):
    type: Literal["turn.state"] = "turn.state"
    turn_id: str
    state: TurnState


class TurnFinished(StreamEvent):
    type: Literal["turn.finished"] = "turn.finished"
    turn_id: str
    kind: TurnKind
    outcome: TurnOutcome
    error: str | None = None


class ActivityChanged(StreamEvent):
    type: Literal["activity.changed"] = "activity.changed"
    turn_id: str
    label: str | None


class WorkSummary(StreamEvent):
    type: Literal["work.summary"] = "work.summary"
    turn_id: str
    item_id: str
    text: str


class SpeechStarted(StreamEvent):
    type: Literal["speech.started"] = "speech.started"
    turn_id: str
    item_id: str
    phase: SpeechPhase


class SpeechUpdated(StreamEvent):
    type: Literal["speech.updated"] = "speech.updated"
    turn_id: str
    item_id: str
    phase: SpeechPhase
    text: str = Field(description="The complete provisional text so far.")


class MessageCommitted(StreamEvent):
    type: Literal["message.committed"] = "message.committed"
    message: MessageView
    notify: NotifyLevel | None = None


class InputAccepted(StreamEvent):
    type: Literal["input.accepted"] = "input.accepted"
    client_message_id: str | None
    message_id: int
    disposition: InputDisposition


class QuestionAsked(StreamEvent):
    type: Literal["question.asked"] = "question.asked"
    question: QuestionView


class QuestionSettled(StreamEvent):
    type: Literal["question.settled"] = "question.settled"
    question: QuestionView


class ReactionsChanged(StreamEvent):
    type: Literal["reactions.changed"] = "reactions.changed"
    message_id: int
    reactions: tuple[str, ...]


class ConversationReset(StreamEvent):
    type: Literal["conversation.reset"] = "conversation.reset"
    reason: Literal["new", "settings", "continuity_lost"]


class StatusChanged(StreamEvent):
    type: Literal["status.changed"] = "status.changed"


class Notice(StreamEvent):
    type: Literal["notice"] = "notice"
    level: Literal["info", "error"]
    text: str


type ServerEvent = Annotated[
    Snapshot
    | Heartbeat
    | TurnStarted
    | TurnStateChanged
    | TurnFinished
    | ActivityChanged
    | WorkSummary
    | SpeechStarted
    | SpeechUpdated
    | MessageCommitted
    | InputAccepted
    | QuestionAsked
    | QuestionSettled
    | ReactionsChanged
    | ConversationReset
    | StatusChanged
    | Notice,
    Field(discriminator="type"),
]

DURABLE_EVENTS: tuple[type[StreamEvent], ...] = (
    TurnStarted,
    TurnFinished,
    MessageCommitted,
    InputAccepted,
    QuestionAsked,
    QuestionSettled,
    ReactionsChanged,
    ConversationReset,
)
