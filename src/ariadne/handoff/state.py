"""Durable handoffs in Ariadne's private Telegram SQLite state."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .models import ConversationHandoff, HandoffStatus

STATE_ENVIRONMENT = "ARIADNE_TELEGRAM_STATE"
ACTIVATION_KEY_ENVIRONMENT = "ARIADNE_ACTIVATION_KEY"
ACTIVATION_SOURCE_ENVIRONMENT = "ARIADNE_ACTIVATION_SOURCE"
MAX_HANDOFF_BODY_LENGTH = 65_536
MAX_ACTIVATION_KEY_LENGTH = 512
MAX_SOURCE_LENGTH = 64
MAX_ERROR_LENGTH = 1_000


class HandoffError(ValueError):
    """A handoff operation could not preserve its lifecycle guarantees."""


class HandoffState:
    """Cross-process staging and delivery state for conversational handoffs."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = path
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"handoff_{uuid.uuid4().hex}")
        self._initialized = False

    def initialize(self, *, recover_claimed: bool = True) -> None:
        """Create the additive schema and conservatively recover interrupted claims."""
        if self._initialized and not recover_claimed:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        now = self._clock()
        with self._connect_unchecked() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_handoffs (
                    id TEXT PRIMARY KEY,
                    activation_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'staged','ready','claimed','completed','discarded'
                    )),
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    released_at REAL,
                    claimed_at REAL,
                    completed_at REAL
                )
                """
            )
            database.execute(
                """
                CREATE INDEX IF NOT EXISTS telegram_handoffs_ready
                ON telegram_handoffs(status, created_at, id)
                """
            )
            if recover_claimed:
                database.execute(
                    """
                    UPDATE telegram_handoffs
                    SET status = 'ready',
                        error = 'Interrupted before shared conversation completed',
                        claimed_at = NULL, updated_at = ?
                    WHERE status = 'claimed'
                    """,
                    (now,),
                )
        self._initialized = True

    def stage(
        self,
        *,
        activation_key: str,
        source: str,
        body: str,
    ) -> ConversationHandoff:
        """Stage or replace the one unreleased handoff for an activation."""
        activation_key = _bounded_required(
            activation_key, "activation key", MAX_ACTIVATION_KEY_LENGTH
        )
        source = _bounded_required(source, "source", MAX_SOURCE_LENGTH)
        body = _bounded_required(body, "handoff", MAX_HANDOFF_BODY_LENGTH)
        now = self._clock()
        identifier = self._id_factory()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            existing = database.execute(
                "SELECT status FROM telegram_handoffs WHERE activation_key = ?",
                (activation_key,),
            ).fetchone()
            if existing is not None and existing["status"] not in {
                "staged",
                "discarded",
            }:
                raise HandoffError(
                    "This activation's handoff has already been released."
                )
            database.execute(
                """
                INSERT INTO telegram_handoffs (
                    id, activation_key, source, body, status, error,
                    created_at, updated_at, released_at, claimed_at, completed_at
                ) VALUES (?, ?, ?, ?, 'staged', NULL, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(activation_key) DO UPDATE SET
                    id = excluded.id,
                    source = excluded.source,
                    body = excluded.body,
                    status = 'staged',
                    error = NULL,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    released_at = NULL,
                    claimed_at = NULL,
                    completed_at = NULL
                """,
                (identifier, activation_key, source, body, now, now),
            )
        handoff = self.for_activation(activation_key)
        assert handoff is not None
        return handoff

    def for_activation(self, activation_key: str) -> ConversationHandoff | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM telegram_handoffs WHERE activation_key = ?",
                (activation_key,),
            ).fetchone()
        return _handoff(row) if row is not None else None

    def get(self, identifier: str) -> ConversationHandoff | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM telegram_handoffs WHERE id = ?", (identifier,)
            ).fetchone()
        return _handoff(row) if row is not None else None

    def release(self, activation_key: str) -> ConversationHandoff | None:
        """Make a staged handoff visible after its activation commits."""
        now = self._clock()
        with self._connect() as database:
            database.execute(
                """
                UPDATE telegram_handoffs
                SET status = 'ready', error = NULL, released_at = ?, updated_at = ?
                WHERE activation_key = ? AND status = 'staged'
                """,
                (now, now, activation_key),
            )
        return self.for_activation(activation_key)

    def discard(self, activation_key: str, error: str | None = None) -> None:
        """Suppress anything staged by a failed or cancelled activation."""
        now = self._clock()
        detail = error.strip()[:MAX_ERROR_LENGTH] if error and error.strip() else None
        with self._connect() as database:
            database.execute(
                """
                UPDATE telegram_handoffs
                SET status = 'discarded', error = ?, updated_at = ?
                WHERE activation_key = ? AND status = 'staged'
                """,
                (detail, now, activation_key),
            )

    def claim_ready(self, *, limit: int = 8) -> tuple[ConversationHandoff, ...]:
        """Claim the oldest bounded batch in FIFO order."""
        if not 1 <= limit <= 32:
            raise HandoffError("Handoff batch limit must be between 1 and 32.")
        now = self._clock()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                """
                SELECT * FROM telegram_handoffs
                WHERE status = 'ready'
                ORDER BY created_at, id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            identifiers = tuple(str(row["id"]) for row in rows)
            if identifiers:
                placeholders = ",".join("?" for _ in identifiers)
                database.execute(
                    f"""
                    UPDATE telegram_handoffs
                    SET status = 'claimed', claimed_at = ?, updated_at = ?, error = NULL
                    WHERE status = 'ready' AND id IN ({placeholders})
                    """,
                    (now, now, *identifiers),
                )
                rows = database.execute(
                    f"SELECT * FROM telegram_handoffs WHERE id IN ({placeholders})",
                    identifiers,
                ).fetchall()
        by_id = {str(row["id"]): _handoff(row) for row in rows}
        return tuple(by_id[identifier] for identifier in identifiers)

    def complete(self, identifiers: Sequence[str]) -> None:
        """Mark a successfully presented batch complete."""
        self._finish_claim(identifiers, completed=True, error=None)

    def retry(self, identifiers: Sequence[str], error: str | None = None) -> None:
        """Return an unsuccessful shared turn to the ready queue."""
        self._finish_claim(identifiers, completed=False, error=error)

    def _finish_claim(
        self,
        identifiers: Sequence[str],
        *,
        completed: bool,
        error: str | None,
    ) -> None:
        unique = tuple(
            dict.fromkeys(identifier for identifier in identifiers if identifier)
        )
        if not unique:
            return
        now = self._clock()
        detail = error.strip()[:MAX_ERROR_LENGTH] if error and error.strip() else None
        placeholders = ",".join("?" for _ in unique)
        with self._connect() as database:
            cursor = database.execute(
                f"""
                UPDATE telegram_handoffs
                SET status = ?, error = ?, updated_at = ?,
                    claimed_at = CASE WHEN ? THEN claimed_at ELSE NULL END,
                    completed_at = CASE WHEN ? THEN ? ELSE NULL END
                WHERE status = 'claimed' AND id IN ({placeholders})
                """,
                (
                    "completed" if completed else "ready",
                    detail,
                    now,
                    completed,
                    completed,
                    now,
                    *unique,
                ),
            )
        if cursor.rowcount != len(unique):
            raise HandoffError("The handoff batch is no longer fully claimed.")

    def list_status(self, status: HandoffStatus) -> tuple[ConversationHandoff, ...]:
        """Inspect one operational lifecycle state in deterministic order."""
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT * FROM telegram_handoffs WHERE status = ?
                ORDER BY created_at, id
                """,
                (status,),
            ).fetchall()
        return tuple(_handoff(row) for row in rows)

    def _connect(self) -> sqlite3.Connection:
        self.initialize(recover_claimed=False)
        return self._connect_unchecked()

    def _connect_unchecked(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database


def _bounded_required(value: str, name: str, limit: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise HandoffError(f"A {name} must not be empty.")
    if len(normalized) > limit:
        raise HandoffError(f"The {name} exceeds its {limit}-character limit.")
    return normalized


def _at(value: object) -> datetime | None:
    return (
        datetime.fromtimestamp(cast(float, value), UTC) if value is not None else None
    )


def _handoff(row: sqlite3.Row) -> ConversationHandoff:
    created_at = _at(row["created_at"])
    updated_at = _at(row["updated_at"])
    assert created_at is not None and updated_at is not None
    return ConversationHandoff(
        id=str(row["id"]),
        activation_key=str(row["activation_key"]),
        source=str(row["source"]),
        body=str(row["body"]),
        status=cast(HandoffStatus, row["status"]),
        error=cast(str | None, row["error"]),
        created_at=created_at,
        updated_at=updated_at,
        released_at=_at(row["released_at"]),
        claimed_at=_at(row["claimed_at"]),
        completed_at=_at(row["completed_at"]),
    )
