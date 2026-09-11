"""Truthful, bounded state for Telegram's deterministic status panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from ..revisit.models import Revisit
from ..stewardship.models import StewardshipRunResult, StewardshipRuntimeSnapshot

WAKEUPS_PAGE_SIZE = 5
MAX_NOTE_CHARS = 90
MAX_ERROR_CHARS = 120

STATUS_CALLBACK_PREFIX = "status:"
STATUS_ROOT_CALLBACK = "status:root"
STATUS_INITIATIVE_CALLBACK = "status:init"
STATUS_INITIATIVE_RUN_CALLBACK = "status:init:run"
STATUS_INITIATIVE_PAUSE_DAY_CALLBACK = "status:init:pause24"
STATUS_INITIATIVE_PAUSE_CALLBACK = "status:init:pause"
STATUS_INITIATIVE_RESUME_CALLBACK = "status:init:resume"
STATUS_DELIVER_CALLBACK = "status:deliver"
STATUS_SETTINGS_CALLBACK = "status:settings"
STATUS_WAKEUPS_PREFIX = "status:wake:"
STATUS_WAKEUP_ASK_PREFIX = "status:cancel:"
STATUS_WAKEUP_CONFIRM_PREFIX = "status:drop:"


class InitiativeControls(Protocol):
    """The stewardship surface a deterministic control panel may touch."""

    def status_snapshot(self) -> StewardshipRuntimeSnapshot: ...

    def pause(self, *, until: datetime | None = None) -> StewardshipRuntimeSnapshot: ...

    def resume(self) -> StewardshipRuntimeSnapshot: ...

    async def process_due(self, *, force: bool = False) -> StewardshipRunResult: ...


class WakeupDirectory(Protocol):
    """The one-off revisit surface a deterministic control panel may touch."""

    def list_open(self) -> tuple[Revisit, ...]: ...

    def cancel(self, identifier: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PrivateSource:
    """One major private capability and whether it is switched on."""

    name: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class StatusSources:
    """Everything the deterministic status panel is allowed to read."""

    timezone: ZoneInfo
    sources: tuple[PrivateSource, ...] = ()
    initiative: InitiativeControls | None = None
    wakeups: WakeupDirectory | None = None


@dataclass(frozen=True, slots=True)
class WakeupCounts:
    """Bounded counts shown on the compact status entry point."""

    upcoming: int
    failed: int


def wakeup_counts(revisits: tuple[Revisit, ...]) -> WakeupCounts:
    """Separate work still expected to run from work that already failed."""
    return WakeupCounts(
        upcoming=sum(1 for item in revisits if item.status in {"pending", "running"}),
        failed=sum(1 for item in revisits if item.status == "failed"),
    )


def page_bounds(
    total: int, page: int, *, size: int = WAKEUPS_PAGE_SIZE
) -> tuple[int, int, int]:
    """Clamp a requested page to the available range and return its slice."""
    pages = max(1, -(-total // size))
    current = min(max(page, 1), pages)
    start = (current - 1) * size
    return current, pages, start


def local_time(value: datetime, timezone: ZoneInfo) -> str:
    """Render one instant the way the owner reads their own calendar."""
    return value.astimezone(timezone).strftime("%a %d %b, %H:%M")


def _shorten(value: str, limit: int) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1].rstrip()}…"


def initiative_lines(
    snapshot: StewardshipRuntimeSnapshot | None,
    timezone: ZoneInfo,
) -> list[str]:
    """Describe recurrence honestly, including pauses and the last failure."""
    if snapshot is None or not snapshot.enabled:
        return ["Initiative: off"]

    if snapshot.running:
        state = "running now"
    elif snapshot.paused and snapshot.paused_until is not None:
        state = f"paused until {local_time(snapshot.paused_until, timezone)}"
    elif snapshot.paused:
        state = "paused"
    else:
        state = "ready"
    lines = [f"Initiative: {state}"]

    lines.append(
        f"Last cycle: {local_time(snapshot.last_completed_at, timezone)}"
        if snapshot.last_completed_at is not None
        else "Last cycle: none yet"
    )
    lines.append(
        f"Next expected: {local_time(snapshot.next_expected_at, timezone)}"
        if snapshot.next_expected_at is not None
        else "Next expected: not scheduled"
    )
    if snapshot.last_error:
        lines.append(f"Last error: {_shorten(snapshot.last_error, MAX_ERROR_CHARS)}")
    return lines


def render_status(
    *,
    working: bool,
    snapshot: StewardshipRuntimeSnapshot | None,
    counts: WakeupCounts | None,
    waiting_handoffs: int,
    sources: tuple[PrivateSource, ...],
    model: str,
    effort: str,
    web_search: str,
    timezone: ZoneInfo,
) -> str:
    """Render the compact entry point without claiming unmeasured connectivity."""
    lines = [
        "Ariadne status",
        "",
        f"Iris: {'working on a turn' if working else 'ready'}",
        "",
        *initiative_lines(snapshot, timezone),
        "",
    ]
    if counts is None:
        lines.append("Wake-ups: unavailable")
    else:
        wakeups = f"Wake-ups: {counts.upcoming} upcoming"
        if counts.failed:
            wakeups += f", {counts.failed} failed"
        lines.append(wakeups)
    lines.append(f"Waiting updates: {waiting_handoffs}")

    if sources:
        enabled = [source.name for source in sources if source.enabled]
        lines += [
            "",
            f"Enabled: {', '.join(enabled) if enabled else 'none'}",
            "Enabled means configured; connectivity is not measured here.",
        ]
    lines += [
        "",
        f"Telegram: {model}, {effort} effort, web {web_search}",
        f"Times are {timezone.key}.",
    ]
    return "\n".join(lines)


def render_wakeups(
    revisits: tuple[Revisit, ...],
    *,
    page: int,
    timezone: ZoneInfo,
) -> tuple[str, tuple[Revisit, ...], int, int]:
    """Render one bounded chronological page and the items it actually shows."""
    if not revisits:
        return ("Wake-ups\n\nNothing is scheduled.", (), 1, 1)

    current, pages, start = page_bounds(len(revisits), page)
    shown = revisits[start : start + WAKEUPS_PAGE_SIZE]
    lines = ["Wake-ups", ""]
    for offset, revisit in enumerate(shown, start=1):
        heading = f"{offset}. {local_time(revisit.due_at, timezone)}"
        heading += f" - {revisit.attention.value}"
        if revisit.status == "failed":
            heading += " - failed"
        elif revisit.status == "running":
            heading += " - running"
        lines.append(heading)
        lines.append(f"   {_shorten(revisit.note, MAX_NOTE_CHARS)}")
        if revisit.status == "failed" and revisit.error:
            lines.append(f"   {_shorten(revisit.error, MAX_ERROR_CHARS)}")
        lines.append("")
    lines.append(f"Page {current} of {pages}, {len(revisits)} open.")
    lines.append("Ask me in conversation to reschedule or reword one.")
    return ("\n".join(lines), shown, current, pages)


def render_wakeup_cancellation(revisit: Revisit, timezone: ZoneInfo) -> str:
    """Name the exact wake-up a trusted cancellation would remove."""
    lines = [
        "Cancel this wake-up?",
        "",
        f"{local_time(revisit.due_at, timezone)} - {revisit.attention.value}",
        _shorten(revisit.note, MAX_NOTE_CHARS),
    ]
    return "\n".join(lines)


def render_initiative(
    snapshot: StewardshipRuntimeSnapshot | None,
    timezone: ZoneInfo,
) -> str:
    """Render the initiative sub-panel and the window it actually runs in."""
    if snapshot is None:
        return "Initiative\n\nDaily initiative is not configured."
    lines = ["Initiative", "", *initiative_lines(snapshot, timezone)]
    if snapshot.enabled:
        lines += [
            "",
            f"Waking window {snapshot.waking_start:%H:%M}"
            f"-{snapshot.waking_end:%H:%M} {snapshot.timezone}.",
        ]
    return "\n".join(lines)
