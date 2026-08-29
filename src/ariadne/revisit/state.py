"""Durable SQLite state for one-off future revisits."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .models import Attention, Revisit, RevisitStatus

STATE_ENVIRONMENT = "ARIADNE_REVISIT_STATE"


class RevisitError(ValueError):
    """A revisit operation could not be completed as requested."""


class RevisitState:
    """A small cross-process store shared by the runtime and MCP capability."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"revisit_{uuid.uuid4().hex}")

    def initialize(self, *, recover_running: bool = True) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS revisits (
                    id TEXT PRIMARY KEY,
                    due_at REAL NOT NULL,
                    note TEXT NOT NULL,
                    attention TEXT NOT NULL
                        CHECK(attention IN ('light','focused','deep')),
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','running','completed','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                )
                """
            )
            if recover_running:
                database.execute(
                    """
                    UPDATE revisits
                    SET status = 'pending', error = 'Interrupted before completion',
                        updated_at = ?
                    WHERE status = 'running'
                    """,
                    (now,),
                )

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database

    def schedule(self, *, due_at: datetime, note: str, attention: Attention) -> Revisit:
        normalized_note = note.strip()
        if not normalized_note:
            raise RevisitError("A revisit needs a useful note for your future self.")
        due_timestamp = _timestamp(due_at)
        now = self._clock()
        if due_timestamp <= now:
            raise RevisitError("A revisit must be scheduled for a future time.")
        identifier = self._id_factory()
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO revisits
                    (id, due_at, note, attention, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    identifier,
                    due_timestamp,
                    normalized_note,
                    attention.value,
                    now,
                    now,
                ),
            )
        revisit = self.get(identifier)
        assert revisit is not None
        return revisit

    def get(self, identifier: str) -> Revisit | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM revisits WHERE id = ?", (identifier,)
            ).fetchone()
        return _revisit(row) if row is not None else None

    def list_open(self) -> tuple[Revisit, ...]:
        """List actionable revisits; completed history remains internal."""
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT * FROM revisits
                WHERE status IN ('pending', 'running', 'failed')
                ORDER BY due_at, created_at
                """
            ).fetchall()
        return tuple(_revisit(row) for row in rows)

    def change(
        self,
        identifier: str,
        *,
        due_at: datetime | None = None,
        note: str | None = None,
        attention: Attention | None = None,
    ) -> Revisit:
        if due_at is None and note is None and attention is None:
            raise RevisitError("Supply a new time, note, or attention level.")
        normalized_note = note.strip() if note is not None else None
        if normalized_note == "":
            raise RevisitError("A revisit needs a useful note for your future self.")
        due_timestamp = _timestamp(due_at) if due_at else None
        now = self._clock()
        if due_timestamp is not None and due_timestamp <= now:
            raise RevisitError("A revisit must be scheduled for a future time.")

        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM revisits WHERE id = ?", (identifier,)
            ).fetchone()
            if row is None:
                raise RevisitError(f"Revisit {identifier!r} does not exist.")
            if row["status"] not in {"pending", "failed"}:
                raise RevisitError("Only a pending or failed revisit can be changed.")
            database.execute(
                """
                UPDATE revisits
                SET due_at = ?, note = ?, attention = ?, status = 'pending',
                    error = NULL, completed_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    due_timestamp if due_timestamp is not None else row["due_at"],
                    normalized_note if normalized_note is not None else row["note"],
                    attention.value if attention is not None else row["attention"],
                    now,
                    identifier,
                ),
            )
        revisit = self.get(identifier)
        assert revisit is not None
        return revisit

    def cancel(self, identifier: str) -> None:
        with self._connect() as database:
            row = database.execute(
                "SELECT status FROM revisits WHERE id = ?", (identifier,)
            ).fetchone()
            if row is None:
                raise RevisitError(f"Revisit {identifier!r} does not exist.")
            if row["status"] not in {"pending", "failed"}:
                raise RevisitError("Only a pending or failed revisit can be cancelled.")
            database.execute("DELETE FROM revisits WHERE id = ?", (identifier,))

    def claim_due(self, *, now: datetime | None = None) -> Revisit | None:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).timestamp()
        updated_at = self._clock()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                """
                SELECT id FROM revisits
                WHERE status = 'pending' AND due_at <= ?
                ORDER BY due_at, created_at
                LIMIT 1
                """,
                (timestamp,),
            ).fetchone()
            if row is None:
                return None
            identifier = cast(str, row["id"])
            database.execute(
                """
                UPDATE revisits
                SET status = 'running', attempts = attempts + 1,
                    error = NULL, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (updated_at, identifier),
            )
            claimed = database.execute(
                "SELECT * FROM revisits WHERE id = ?", (identifier,)
            ).fetchone()
        assert claimed is not None
        return _revisit(claimed)

    def complete(self, identifier: str) -> None:
        now = self._clock()
        self._finish(identifier, "completed", None, now)

    def fail(self, identifier: str, error: BaseException) -> None:
        self._finish(identifier, "failed", str(error)[:1000], None)

    def release(self, identifier: str) -> None:
        """Return a cancelled in-process execution to the pending queue."""
        now = self._clock()
        with self._connect() as database:
            database.execute(
                """
                UPDATE revisits
                SET status = 'pending', error = 'Interrupted before completion',
                    updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, identifier),
            )

    def _finish(
        self,
        identifier: str,
        status: Literal["completed", "failed"],
        error: str | None,
        completed_at: float | None,
    ) -> None:
        now = self._clock()
        with self._connect() as database:
            cursor = database.execute(
                """
                UPDATE revisits
                SET status = ?, error = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, error, completed_at, now, identifier),
            )
            if cursor.rowcount != 1:
                raise RevisitError("The revisit is not currently running.")


def _at(timestamp: float | None) -> datetime | None:
    return datetime.fromtimestamp(timestamp, UTC) if timestamp is not None else None


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RevisitError("Revisit time must include an explicit timezone offset.")
    return value.astimezone(UTC).timestamp()


def _revisit(row: sqlite3.Row) -> Revisit:
    return Revisit(
        id=cast(str, row["id"]),
        due_at=cast(datetime, _at(cast(float, row["due_at"]))),
        note=cast(str, row["note"]),
        attention=Attention(row["attention"]),
        status=cast(RevisitStatus, row["status"]),
        attempts=cast(int, row["attempts"]),
        error=cast(str | None, row["error"]),
        created_at=cast(datetime, _at(cast(float, row["created_at"]))),
        updated_at=cast(datetime, _at(cast(float, row["updated_at"]))),
        completed_at=_at(cast(float | None, row["completed_at"])),
    )
