"""Small immutable models used by the behaviour scenario lab."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ariadne.mail import MailRoute, parse_metadata
from ariadne.prompts.activations import (
    build_mail_turn_prompt,
    build_revisit_turn_prompt,
)
from ariadne.prompts.mail_evidence import render_mail_evidence
from ariadne.revisit import Attention
from ariadne.telegram.history import (
    TelegramContentType,
    TelegramHistoryMessage,
    TelegramMessageSource,
    TelegramSpeaker,
)


@dataclass(frozen=True, slots=True)
class ScenarioFile:
    """One synthetic file present in the disposable Thread."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class ScenarioKnowledge:
    """One semantic record exposed by the disposable knowledge capability."""

    id: str
    title: str
    summary: str
    kind: str
    collection: str
    body: str
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    starts_at: str | None = None
    ends_at: str | None = None
    related: tuple[tuple[str, str], ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "schema": 1,
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "kind": self.kind,
            "collection": self.collection,
            "tags": list(self.tags),
            "aliases": list(self.aliases),
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "related": [
                {"record": record, "relation": relation}
                for record, relation in self.related
            ],
            "archived": False,
            "body": self.body,
        }


@dataclass(frozen=True, slots=True)
class ScenarioCalendarEvent:
    """One event exposed by the disposable Calendar capability."""

    id: str
    title: str
    start: str
    end: str
    description: str | None = None
    location: str | None = None
    busy: bool = True
    status: str = "confirmed"

    def payload(self) -> dict[str, object]:
        all_day = "T" not in self.start
        return {
            "id": self.id,
            "series_id": self.id,
            "calendar_id": "scenario-calendar",
            "calendar": "Personal",
            "uid": f"{self.id}@ariadne.test",
            "etag": f"{self.id}-etag",
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "all_day": all_day,
            "timezone": None if all_day else "Europe/London",
            "description": self.description,
            "location": self.location,
            "status": self.status,
            "busy": self.busy,
            "recurrence": None,
            "recurrence_id": None,
            "is_occurrence": False,
            "organizer": None,
            "attendees": [],
            "alarms": [],
        }


@dataclass(frozen=True, slots=True)
class ScenarioRevisit:
    """One synthetic due revisit used to wake a behaviour scenario."""

    note: str
    attention: Attention
    scheduled_for: datetime
    awakened_at: datetime


@dataclass(frozen=True, slots=True)
class ScenarioTelegramMessage:
    """One permanent Telegram message visible to a behaviour scenario."""

    message_id: int
    sent_at: datetime
    speaker: TelegramSpeaker
    source: TelegramMessageSource
    text: str
    content_type: TelegramContentType = "text"
    reply_to_message_id: int | None = None

    def stored(self, chat_id: int) -> TelegramHistoryMessage:
        return TelegramHistoryMessage(
            chat_id=chat_id,
            message_id=self.message_id,
            sent_at=self.sent_at,
            speaker=self.speaker,
            source=self.source,
            content_type=self.content_type,
            text=self.text,
            reply_to_message_id=self.reply_to_message_id,
        )


@dataclass(frozen=True, slots=True)
class BehaviorScenario:
    """A production-shaped event with explicit points for human review."""

    identifier: str
    title: str
    description: str
    email: bytes | None
    route: MailRoute | None
    files: tuple[ScenarioFile, ...]
    knowledge: tuple[ScenarioKnowledge, ...]
    calendar: tuple[ScenarioCalendarEvent, ...]
    review_questions: tuple[str, ...]
    telegram: tuple[ScenarioTelegramMessage, ...] = ()
    revisit: ScenarioRevisit | None = None
    telegram_prompt: str | None = None

    def __post_init__(self) -> None:
        if (self.email is None) != (self.route is None):
            raise ValueError("A mail scenario needs both an email and a route.")
        triggers = (
            self.email is not None,
            self.revisit is not None,
            self.telegram_prompt is not None,
        )
        if sum(triggers) != 1:
            raise ValueError("A behaviour scenario needs exactly one trigger.")

    @property
    def profile_name(self) -> str:
        if self.telegram_prompt is not None:
            return "telegram"
        return (
            f"revisit-{self.revisit.attention.value}"
            if self.revisit is not None
            else "mail"
        )

    def turn_input(self, workspace: Path, *, human: str = "Divy") -> str:
        """Render the same user input or Ariadne activation used in production."""
        if self.telegram_prompt is not None:
            return self.telegram_prompt
        if self.revisit is not None:
            scheduled = self.revisit
            return build_revisit_turn_prompt(
                note=scheduled.note,
                due_at=scheduled.scheduled_for,
                awakened_at=scheduled.awakened_at,
                attention=scheduled.attention.value,
                human=human,
            )
        assert self.email is not None
        assert self.route is not None
        metadata = parse_metadata(self.email)
        return build_mail_turn_prompt(
            render_mail_evidence(self.email, metadata),
            route_id=self.route.id,
            route_classification=self.route.classification,
            move_after_iris=None,
            unmatched_keep_in_inbox=True,
        )
