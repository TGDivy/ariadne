"""Read models the app shows outside the conversation."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from ..conversation.core import ConversationCore
from ..handoff import HandoffCoordinator
from ..knowledge.models import KnowledgeError, KnowledgeValidationError
from ..knowledge.store import KnowledgeStore
from ..revisit.models import Revisit
from ..stewardship.models import StewardshipRuntimeSnapshot
from .schemas import (
    ActivityPage,
    FolderView,
    GoalsView,
    GoalView,
    InitiativeView,
    LibraryView,
    RecordRef,
    RecordView,
    ScheduledView,
    SettingsView,
    SourceView,
    StatusView,
    UpcomingView,
)

LOGGER = logging.getLogger(__name__)

GOAL_FOLDER = "goal"
MAX_TITLE_LENGTH = 80
READ_BATCH = 20


class InitiativeStatus(Protocol):
    def status_snapshot(self) -> StewardshipRuntimeSnapshot: ...


class WakeupList(Protocol):
    def list_open(self) -> tuple[Revisit, ...]: ...


class UpcomingReader(Protocol):
    def upcoming(self) -> UpcomingView: ...


class ActivityReader(Protocol):
    def page(self, *, before: str | None, limit: int) -> ActivityPage: ...


def short_title(note: str) -> str:
    """Use the first line of a note as a title that fits one row."""
    line = next((part.strip() for part in note.splitlines() if part.strip()), note)
    if len(line) <= MAX_TITLE_LENGTH:
        return line
    return f"{line[: MAX_TITLE_LENGTH - 1].rstrip()}…"


def scheduled_wakeup(revisit: Revisit) -> ScheduledView:
    status = revisit.status if revisit.status != "completed" else "pending"
    return ScheduledView(
        id=revisit.id,
        kind="wakeup",
        title=short_title(revisit.note),
        note=revisit.note,
        due_at=revisit.due_at,
        attention=revisit.attention.value,
        status=status,
        error=revisit.error,
    )


@dataclass(frozen=True, slots=True)
class RuntimeUpcoming:
    """Scheduled wake-ups plus the next daily initiative, if one is expected."""

    wakeups: WakeupList | None
    initiative: InitiativeStatus | None

    def upcoming(self) -> UpcomingView:
        items: list[ScheduledView] = []
        if self.wakeups is not None:
            try:
                items.extend(
                    scheduled_wakeup(item) for item in self.wakeups.list_open()
                )
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception("Wake-up list read failed")
        snapshot = _initiative_snapshot(self.initiative)
        if snapshot is not None and snapshot.enabled and not snapshot.paused:
            items.append(
                ScheduledView(
                    id="daily",
                    kind="daily",
                    title="Daily initiative",
                    due_at=snapshot.next_expected_at,
                    status="running" if snapshot.running else "pending",
                    error=snapshot.last_error,
                )
            )
        return UpcomingView(items=tuple(sorted(items, key=_due_order)))


class NoActivity:
    """The Activity trail before activations record it."""

    def page(self, *, before: str | None, limit: int) -> ActivityPage:
        return ActivityPage(items=(), has_more=False)


@dataclass(frozen=True, slots=True)
class StatusReader:
    """Truthful, bounded status: what is enabled, not what is reachable."""

    core: ConversationCore
    handoffs: HandoffCoordinator
    timezone: ZoneInfo
    sources: tuple[SourceView, ...]
    wakeups: WakeupList | None = None
    initiative: InitiativeStatus | None = None

    def status(self) -> StatusView:
        upcoming = failed = 0
        if self.wakeups is not None:
            try:
                open_wakeups = self.wakeups.list_open()
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception("Wake-up count read failed")
                open_wakeups = ()
            upcoming = sum(1 for item in open_wakeups if item.status != "failed")
            failed = sum(1 for item in open_wakeups if item.status == "failed")
        try:
            waiting = self.handoffs.waiting_count()
        except (OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Waiting-handoff count failed")
            waiting = 0
        settings = self.core.settings
        snapshot = _initiative_snapshot(self.initiative)
        return StatusView(
            state="working" if self.core.busy else "idle",
            turn_kind=self.core.turn_kind,
            activity=self.core.live_activity,
            wakeups_upcoming=upcoming,
            wakeups_failed=failed,
            waiting_handoffs=waiting,
            initiative=_initiative_view(snapshot) if snapshot is not None else None,
            sources=self.sources,
            settings=SettingsView(
                model=settings.model,
                effort=settings.effort.value,
                web_search=settings.web_search,
            ),
            timezone=self.timezone.key,
        )


class ThreadReader:
    """Read Goals and Library views from the Thread without changing it."""

    def __init__(self, root: Path) -> None:
        self._store = KnowledgeStore(root)

    def goals(self) -> GoalsView:
        active_refs = self._list_records(GOAL_FOLDER, archived=False)
        completed = self._list_records(GOAL_FOLDER, archived=True)
        goals: list[GoalView] = []
        for batch in _batches([ref.id for ref in active_refs], READ_BATCH):
            for record in self._store.read(batch):
                goals.append(
                    GoalView(
                        id=record.metadata.id,
                        title=record.metadata.title,
                        summary=record.metadata.summary,
                        folder=record.folder,
                    )
                )
        return GoalsView(active=tuple(goals), completed=completed)

    def library(self, folder: str, *, archived: bool) -> LibraryView:
        listing = self._store.list_folder(folder=folder, archived=archived)
        return LibraryView(
            folder=listing.folder,
            archived=listing.archived,
            folders=tuple(
                FolderView(folder=item.folder, record_count=item.record_count)
                for item in listing.folders
            ),
            records=tuple(
                RecordRef(id=item.id, title=item.title) for item in listing.records
            ),
            truncated=listing.folders_truncated or listing.records_truncated,
        )

    def record(self, identifier: str) -> RecordView | None:
        try:
            (record,) = self._store.read([identifier])
        except KnowledgeValidationError:
            return None
        return RecordView(
            id=record.metadata.id,
            title=record.metadata.title,
            summary=record.metadata.summary,
            folder=record.folder,
            archived=record.archived,
            aliases=record.metadata.aliases,
            body=record.body,
            links=tuple(
                RecordRef(id=link.id, title=link.title) for link in record.links
            ),
        )

    def _list_records(self, folder: str, *, archived: bool) -> tuple[RecordRef, ...]:
        try:
            listing = self._store.list_folder(folder=folder, archived=archived)
        except KnowledgeError:
            return ()
        return tuple(
            RecordRef(id=item.id, title=item.title) for item in listing.records
        )


def _initiative_snapshot(
    initiative: InitiativeStatus | None,
) -> StewardshipRuntimeSnapshot | None:
    if initiative is None:
        return None
    try:
        return initiative.status_snapshot()
    except (OSError, sqlite3.Error, ValueError):
        LOGGER.exception("Initiative status read failed")
        return None


def _initiative_view(snapshot: StewardshipRuntimeSnapshot) -> InitiativeView:
    return InitiativeView(
        enabled=snapshot.enabled,
        paused=snapshot.paused,
        paused_until=snapshot.paused_until,
        running=snapshot.running,
        last_completed_at=snapshot.last_completed_at,
        next_expected_at=snapshot.next_expected_at,
        last_error=snapshot.last_error,
        timezone=snapshot.timezone,
        waking_start=snapshot.waking_start.isoformat(timespec="minutes"),
        waking_end=snapshot.waking_end.isoformat(timespec="minutes"),
    )


def _due_order(item: ScheduledView) -> tuple[bool, datetime | None, str]:
    return (item.due_at is None, item.due_at, item.id)


def _batches(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]
