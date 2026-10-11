"""Request and response models for the Ari app API.

These models, together with the stream events in `conversation.events`, are
the contract the app is built against. `ariadne.app.contract` renders them to
a checked-in OpenAPI document.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ..codex.models import WebSearchSetting
from ..conversation.events import MessageView, QuestionView, WireModel
from ..conversation.models import InputDisposition, TurnKind
from ..conversation.questions import SelectionOutcome

ActivationKind = Literal["wakeup", "delegated", "daily", "weekly"]
Attention = Literal["light", "focused", "deep"]
ActivityOutcome = Literal[
    "running", "spoke", "handed_off", "quiet", "failed", "stopped", "skipped"
]


class ErrorDetail(WireModel):
    code: str
    message: str


class ErrorResponse(WireModel):
    error: ErrorDetail


class OkResponse(WireModel):
    ok: Literal[True] = True


class IdentityView(WireModel):
    agent_name: str
    human_name: str
    avatar_url: str | None = None


class SendMessageRequest(WireModel):
    client_message_id: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=20_000)
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=10)
    reply_to_id: int | None = None
    voice: bool = False

    @model_validator(mode="after")
    def require_content(self) -> SendMessageRequest:
        if not self.text.strip() and not self.attachment_ids:
            raise ValueError("A message needs text or an attachment.")
        return self


class SendMessageResponse(WireModel):
    message: MessageView
    disposition: InputDisposition


class MessagesPage(WireModel):
    messages: tuple[MessageView, ...]
    has_more: bool


class StopResponse(WireModel):
    stopping: bool


class AnswerRequest(WireModel):
    choice_index: int = Field(ge=0, le=5)


class AnswerResponse(WireModel):
    outcome: SelectionOutcome
    question: QuestionView | None = None


class ReactionsRequest(WireModel):
    reactions: tuple[str, ...] = Field(default=(), max_length=8)


class ReactionsResponse(WireModel):
    message_id: int
    reactions: tuple[str, ...]


class AttachmentUploaded(WireModel):
    id: str
    kind: Literal["image", "document"]
    filename: str
    mime_type: str | None
    size_bytes: int


class ModelView(WireModel):
    id: str
    display_name: str
    default_effort: str
    supported_efforts: tuple[str, ...]


class SettingsView(WireModel):
    model: str
    effort: str
    web_search: WebSearchSetting


class SettingsOptions(WireModel):
    current: SettingsView
    models: tuple[ModelView, ...]


class SettingsUpdate(WireModel):
    model: str | None = None
    effort: str | None = None
    web_search: WebSearchSetting | None = None


class SourceView(WireModel):
    name: str
    enabled: bool


class InitiativeView(WireModel):
    enabled: bool
    paused: bool
    paused_until: datetime | None
    running: bool
    last_completed_at: datetime | None
    next_expected_at: datetime | None
    last_error: str | None
    timezone: str
    waking_start: str
    waking_end: str


class StatusView(WireModel):
    state: Literal["idle", "working"]
    turn_kind: TurnKind | None
    activity: str | None
    wakeups_upcoming: int
    wakeups_failed: int
    waiting_handoffs: int
    initiative: InitiativeView | None
    sources: tuple[SourceView, ...]
    settings: SettingsView
    timezone: str


class RecordRef(WireModel):
    id: str
    title: str


class ScheduledView(WireModel):
    id: str
    kind: ActivationKind
    title: str
    note: str | None = None
    due_at: datetime | None = None
    attention: Attention | None = None
    status: Literal["pending", "running", "failed"]
    error: str | None = None
    about: tuple[RecordRef, ...] = ()


class UpcomingView(WireModel):
    items: tuple[ScheduledView, ...]


class EffectView(WireModel):
    type: str
    label: str
    record_id: str | None = None


class ActivityItemView(WireModel):
    id: str
    kind: ActivationKind | Literal["conversation", "mail"]
    title: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: ActivityOutcome
    account: str | None = None
    effects: tuple[EffectView, ...] = ()
    about: tuple[RecordRef, ...] = ()
    error: str | None = None


class ActivityPage(WireModel):
    items: tuple[ActivityItemView, ...]
    has_more: bool


class GoalView(WireModel):
    id: str
    title: str
    summary: str
    folder: str
    next: ScheduledView | None = None
    last_activity_at: datetime | None = None


class GoalsView(WireModel):
    active: tuple[GoalView, ...]
    completed: tuple[RecordRef, ...]


class FolderView(WireModel):
    folder: str
    record_count: int


class LibraryView(WireModel):
    folder: str
    archived: bool
    folders: tuple[FolderView, ...]
    records: tuple[RecordRef, ...]
    truncated: bool


class RecordView(WireModel):
    id: str
    title: str
    summary: str
    folder: str
    archived: bool
    aliases: tuple[str, ...]
    body: str = Field(description="GitHub-flavoured Markdown.")
    links: tuple[RecordRef, ...]


class DeviceRegistration(WireModel):
    token: str = Field(pattern=r"^[0-9a-fA-F]{32,256}$")
    environment: Literal["sandbox", "production"]
