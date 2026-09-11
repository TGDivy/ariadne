"""Small immutable records for the daily stewardship pulse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal

StewardshipStatus = Literal["idle", "running", "failed"]
StewardshipRunStatus = Literal["completed", "failed", "not-run"]


@dataclass(frozen=True, slots=True)
class StewardshipCycle:
    """One claimed creative cycle, independent of any later revisit."""

    id: str
    local_day: date
    attempted_at: datetime


@dataclass(frozen=True, slots=True)
class StewardshipSnapshot:
    """The bounded orientation retained between daily cycles."""

    status: StewardshipStatus
    active_cycle_id: str | None
    active_local_day: date | None
    last_attempted_at: datetime | None
    last_attempted_local_day: date | None
    last_completed_at: datetime | None
    last_completed_local_day: date | None
    recent_summary: str
    last_broad_attention_at: datetime | None
    last_broad_attention: str | None
    last_error: str | None
    paused: bool
    paused_until: datetime | None


@dataclass(frozen=True, slots=True)
class StewardshipRunResult:
    """Owner-visible outcome of one scheduler check or explicit run."""

    status: StewardshipRunStatus
    cycle_id: str | None = None
    local_day: date | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StewardshipRuntimeSnapshot:
    """Compact operational status for Telegram and operator surfaces."""

    enabled: bool
    timezone: str
    waking_start: time
    waking_end: time
    paused: bool
    paused_until: datetime | None
    running: bool
    active_cycle_id: str | None
    last_completed_at: datetime | None
    last_completed_local_day: date | None
    next_expected_at: datetime | None
    last_error: str | None
