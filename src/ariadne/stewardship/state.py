"""Minimal durable scheduling and orientation state for stewardship."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from datetime import time as daytime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from .models import StewardshipCycle, StewardshipSnapshot, StewardshipStatus

STATE_ENVIRONMENT = "ARIADNE_STEWARDSHIP_STATE"
CYCLE_ENVIRONMENT = "ARIADNE_STEWARDSHIP_CYCLE"
MAX_OUTCOME_LENGTH = 4_000
MAX_RECENT_SUMMARY_LENGTH = 12_000
MAX_BROAD_ATTENTION_LENGTH = 1_000
MAX_ERROR_LENGTH = 1_000
FAILURE_RETRY_SECONDS = 30 * 60


class StewardshipStateError(ValueError):
    """A stewardship lifecycle or bounded-state request was invalid."""


class StewardshipState:
    """One SQLite singleton; substantive life state remains in source systems."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"stewardship_{uuid.uuid4().hex}")
        self._initialized = False

    def initialize(self, *, recover_running: bool = True) -> tuple[str, ...]:
        """Create state and return any activation interrupted by process death."""
        if self._initialized and not recover_running:
            return ()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        interrupted: tuple[str, ...] = ()
        with self._connect_unchecked() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS stewardship_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    status TEXT NOT NULL
                        CHECK(status IN ('idle','running','failed')),
                    paused INTEGER NOT NULL DEFAULT 0
                        CHECK(paused IN (0, 1)),
                    paused_until REAL,
                    active_cycle_id TEXT,
                    active_local_day TEXT,
                    last_attempted_at REAL,
                    last_attempted_local_day TEXT,
                    last_completed_at REAL,
                    last_completed_local_day TEXT,
                    recent_summary TEXT NOT NULL DEFAULT '',
                    last_broad_attention_at REAL,
                    last_broad_attention TEXT,
                    staged_summary TEXT,
                    staged_broad_attention TEXT,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._migrate_pause_columns(database)
            database.execute(
                """
                INSERT OR IGNORE INTO stewardship_state
                    (singleton, status, updated_at)
                VALUES (1, 'idle', ?)
                """,
                (self._clock(),),
            )
            if recover_running:
                row = database.execute(
                    "SELECT active_cycle_id FROM stewardship_state "
                    "WHERE singleton = 1 AND status = 'running'"
                ).fetchone()
                if row is not None and row["active_cycle_id"] is not None:
                    interrupted = (str(row["active_cycle_id"]),)
                    database.execute(
                        """
                        UPDATE stewardship_state
                        SET status = 'idle', active_cycle_id = NULL,
                            active_local_day = NULL, staged_summary = NULL,
                            staged_broad_attention = NULL,
                            last_error = 'Interrupted before cycle completion',
                            updated_at = ?
                        WHERE singleton = 1
                        """,
                        (self._clock(),),
                    )
        self._initialized = True
        return interrupted

    def claim_due(
        self,
        *,
        now: datetime,
        timezone: ZoneInfo,
        waking_start: daytime,
        waking_end: daytime,
        force: bool = False,
    ) -> StewardshipCycle | None:
        """Claim today's one cycle when inside its local waking window."""
        _require_aware(now)
        local_now = now.astimezone(timezone)
        local_time = local_now.time().replace(tzinfo=None)
        if not force and not waking_start <= local_time < waking_end:
            return None
        now_timestamp = now.astimezone(UTC).timestamp()
        local_day = local_now.date()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM stewardship_state WHERE singleton = 1"
            ).fetchone()
            assert row is not None
            if row["status"] == "running":
                return None
            paused = bool(row["paused"])
            paused_until = (
                float(row["paused_until"]) if row["paused_until"] is not None else None
            )
            if paused and paused_until is not None and paused_until <= now_timestamp:
                database.execute(
                    """
                    UPDATE stewardship_state
                    SET paused = 0, paused_until = NULL, updated_at = ?
                    WHERE singleton = 1
                    """,
                    (now_timestamp,),
                )
                paused = False
            if paused and not force:
                return None
            completed_day = _day(row["last_completed_local_day"])
            if not force and completed_day == local_day:
                return None
            if (
                not force
                and row["status"] == "failed"
                and row["last_attempted_at"] is not None
                and now_timestamp - float(row["last_attempted_at"])
                < FAILURE_RETRY_SECONDS
            ):
                return None
            identifier = self._id_factory()
            database.execute(
                """
                UPDATE stewardship_state
                SET status = 'running', active_cycle_id = ?,
                    active_local_day = ?, last_attempted_at = ?,
                    last_attempted_local_day = ?, staged_summary = NULL,
                    staged_broad_attention = NULL, last_error = NULL,
                    updated_at = ?
                WHERE singleton = 1
                """,
                (
                    identifier,
                    local_day.isoformat(),
                    now_timestamp,
                    local_day.isoformat(),
                    now_timestamp,
                ),
            )
        return StewardshipCycle(identifier, local_day, now.astimezone(UTC))

    def record_outcome(
        self,
        cycle_id: str,
        *,
        summary: str,
        broad_attention: str | None = None,
    ) -> None:
        """Stage the Learn result for commit by the owning runtime."""
        summary = _bounded_required(summary, "cycle summary", MAX_OUTCOME_LENGTH)
        if broad_attention is not None:
            broad_attention = broad_attention.strip() or None
            if (
                broad_attention is not None
                and len(broad_attention) > MAX_BROAD_ATTENTION_LENGTH
            ):
                raise StewardshipStateError(
                    "The broad-attention note exceeds its 1000-character limit."
                )
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE stewardship_state
                SET staged_summary = ?, staged_broad_attention = ?, updated_at = ?
                WHERE singleton = 1 AND status = 'running'
                    AND active_cycle_id = ?
                """,
                (summary, broad_attention, self._clock(), cycle_id),
            )
        if cursor.rowcount != 1:
            raise StewardshipStateError("The stewardship cycle is not running.")

    def complete(self, cycle_id: str, *, fallback_summary: str) -> None:
        """Commit one completed cycle and its bounded recent orientation."""
        fallback_summary = _bounded_required(
            fallback_summary, "fallback cycle summary", MAX_OUTCOME_LENGTH
        )
        now = self._clock()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM stewardship_state WHERE singleton = 1"
            ).fetchone()
            assert row is not None
            if row["status"] != "running" or row["active_cycle_id"] != cycle_id:
                raise StewardshipStateError("The stewardship cycle is not running.")
            summary = str(row["staged_summary"] or fallback_summary).strip()
            local_day = str(row["active_local_day"])
            recent = _append_recent(str(row["recent_summary"]), local_day, summary)
            broad = cast(str | None, row["staged_broad_attention"])
            database.execute(
                """
                UPDATE stewardship_state
                SET status = 'idle', active_cycle_id = NULL,
                    active_local_day = NULL, last_completed_at = ?,
                    last_completed_local_day = ?, recent_summary = ?,
                    last_broad_attention_at = CASE
                        WHEN ? IS NULL THEN last_broad_attention_at ELSE ? END,
                    last_broad_attention = COALESCE(?, last_broad_attention),
                    staged_summary = NULL, staged_broad_attention = NULL,
                    last_error = NULL, updated_at = ?
                WHERE singleton = 1
                """,
                (now, local_day, recent, broad, now, broad, now),
            )

    def fail(self, cycle_id: str, error: BaseException) -> None:
        self._finish_unsuccessfully(cycle_id, "failed", str(error)[:MAX_ERROR_LENGTH])

    def release(self, cycle_id: str) -> None:
        """Make an explicitly cancelled cycle immediately retryable."""
        self._finish_unsuccessfully(cycle_id, "idle", "Cycle was cancelled")

    def pause(self, *, until: datetime | None = None) -> StewardshipSnapshot:
        """Persist an indefinite or future-dated recurrence pause."""
        pause_until: float | None = None
        now = self._clock()
        if until is not None:
            _require_aware(until)
            pause_until = until.astimezone(UTC).timestamp()
            if pause_until <= now:
                raise StewardshipStateError(
                    "A stewardship pause-until time must be in the future."
                )
        with self._connect() as database:
            database.execute(
                """
                UPDATE stewardship_state
                SET paused = 1, paused_until = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (pause_until, now),
            )
        return self.snapshot()

    def resume(self) -> StewardshipSnapshot:
        """Clear any persisted pause without changing the daily cycle history."""
        with self._connect() as database:
            database.execute(
                """
                UPDATE stewardship_state
                SET paused = 0, paused_until = NULL, updated_at = ?
                WHERE singleton = 1
                """,
                (self._clock(),),
            )
        return self.snapshot()

    def _finish_unsuccessfully(
        self,
        cycle_id: str,
        status: StewardshipStatus,
        error: str,
    ) -> None:
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE stewardship_state
                SET status = ?, active_cycle_id = NULL, active_local_day = NULL,
                    staged_summary = NULL, staged_broad_attention = NULL,
                    last_error = ?, updated_at = ?
                WHERE singleton = 1 AND status = 'running'
                    AND active_cycle_id = ?
                """,
                (status, error, self._clock(), cycle_id),
            )
        if cursor.rowcount != 1:
            raise StewardshipStateError("The stewardship cycle is not running.")

    def snapshot(self, *, now: datetime | None = None) -> StewardshipSnapshot:
        timestamp = self._clock()
        if now is not None:
            _require_aware(now)
            timestamp = now.astimezone(UTC).timestamp()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE stewardship_state
                SET paused = 0, paused_until = NULL, updated_at = ?
                WHERE singleton = 1 AND paused = 1
                    AND paused_until IS NOT NULL AND paused_until <= ?
                """,
                (timestamp, timestamp),
            )
            row = database.execute(
                "SELECT * FROM stewardship_state WHERE singleton = 1"
            ).fetchone()
        assert row is not None
        return StewardshipSnapshot(
            status=cast(StewardshipStatus, row["status"]),
            active_cycle_id=cast(str | None, row["active_cycle_id"]),
            active_local_day=_day(row["active_local_day"]),
            last_attempted_at=_at(row["last_attempted_at"]),
            last_attempted_local_day=_day(row["last_attempted_local_day"]),
            last_completed_at=_at(row["last_completed_at"]),
            last_completed_local_day=_day(row["last_completed_local_day"]),
            recent_summary=str(row["recent_summary"]),
            last_broad_attention_at=_at(row["last_broad_attention_at"]),
            last_broad_attention=cast(str | None, row["last_broad_attention"]),
            last_error=cast(str | None, row["last_error"]),
            paused=bool(row["paused"]),
            paused_until=_at(row["paused_until"]),
        )

    @staticmethod
    def _migrate_pause_columns(database: sqlite3.Connection) -> None:
        """Add pause controls to state created by an earlier build."""
        columns = {
            str(row["name"])
            for row in database.execute(
                "PRAGMA table_info(stewardship_state)"
            ).fetchall()
        }
        if "paused" not in columns:
            database.execute(
                "ALTER TABLE stewardship_state "
                "ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
            )
        if "paused_until" not in columns:
            database.execute(
                "ALTER TABLE stewardship_state ADD COLUMN paused_until REAL"
            )

    def _connect(self) -> sqlite3.Connection:
        self.initialize(recover_running=False)
        return self._connect_unchecked()

    def _connect_unchecked(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database


def _append_recent(existing: str, local_day: str, summary: str) -> str:
    entry = f"{local_day}: {summary}"
    combined = f"{existing.strip()}\n{entry}".strip()
    if len(combined) <= MAX_RECENT_SUMMARY_LENGTH:
        return combined
    return combined[-MAX_RECENT_SUMMARY_LENGTH:].lstrip()


def _bounded_required(value: str, name: str, limit: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise StewardshipStateError(f"A {name} must not be empty.")
    if len(normalized) > limit:
        raise StewardshipStateError(f"The {name} exceeds its {limit}-character limit.")
    return normalized


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StewardshipStateError("Stewardship times need an explicit timezone.")


def _at(value: object) -> datetime | None:
    return (
        datetime.fromtimestamp(float(cast(float, value)), UTC)
        if value is not None
        else None
    )


def _day(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value is not None else None
