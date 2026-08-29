"""Small immutable models used by the behaviour scenario lab."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ariadne.mail import MailRoute, build_mail_turn_prompt, parse_metadata
from ariadne.mail.models import MailJob
from ariadne.revisit import Attention, Revisit
from ariadne.revisit.runtime import build_revisit_turn_prompt


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
    revisit: ScenarioRevisit | None = None

    def __post_init__(self) -> None:
        mail_trigger = self.email is not None and self.route is not None
        revisit_trigger = self.revisit is not None
        if mail_trigger == revisit_trigger:
            raise ValueError("A behaviour scenario needs exactly one trigger.")

    @property
    def profile_name(self) -> str:
        return (
            f"revisit-{self.revisit.attention.value}"
            if self.revisit is not None
            else "mail"
        )

    def turn_input(self, workspace: Path, *, human: str = "Divy") -> str:
        """Render the same owner activation used by the production runtime."""
        if self.revisit is not None:
            scheduled = self.revisit
            created_at = scheduled.scheduled_for.astimezone(UTC)
            return build_revisit_turn_prompt(
                Revisit(
                    id=f"revisit_{self.identifier}",
                    due_at=scheduled.scheduled_for,
                    note=scheduled.note,
                    attention=scheduled.attention,
                    status="running",
                    attempts=1,
                    error=None,
                    created_at=created_at,
                    updated_at=scheduled.awakened_at.astimezone(UTC),
                    completed_at=None,
                ),
                awakened_at=scheduled.awakened_at,
                human=human,
            )
        assert self.email is not None
        assert self.route is not None
        metadata = parse_metadata(self.email)
        job = MailJob(
            job_id=f"INBOX:1:{self.identifier}",
            mailbox="INBOX",
            uidvalidity=1,
            uid=1,
            message_id=metadata.message_id,
            status="running",
            attempts=1,
            route_id=self.route.id,
            action=None,
            destination=None,
            classification=None,
            importance=None,
            suggested_action=None,
            draft_reply=None,
        )
        return build_mail_turn_prompt(
            job,
            metadata,
            self.email,
            route=self.route,
            move_after_iris=None,
            routes_path=workspace / "mail-routes.yaml",
            unmatched_keep_in_inbox=True,
        )
