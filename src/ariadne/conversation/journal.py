"""A bounded journal of committed events so clients can resume a stream."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import connect, prepare

JOURNAL_RETAINED_EVENTS = 5_000
PRUNE_EVERY_APPENDS = 200


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One committed event with its monotonic sequence number."""

    seq: int
    payload: dict[str, Any]


class EventJournal:
    """Persist durable events in commit order; streaming deltas never enter."""

    def __init__(self, path: Path, *, retained: int = JOURNAL_RETAINED_EVENTS) -> None:
        if retained < 1:
            raise ValueError("The journal must retain at least one event.")
        self.path = path
        self._retained = retained
        self._appends = 0
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        prepare(self.path)
        with connect(self.path) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def append(self, payload: dict[str, Any]) -> int:
        """Store one event and return its sequence number."""
        with self._connect() as database:
            cursor = database.execute(
                "INSERT INTO conversation_events (payload, created_at) VALUES (?, ?)",
                (json.dumps(payload, ensure_ascii=False, default=str), time.time()),
            )
            assert cursor.lastrowid is not None
            seq = cursor.lastrowid
        self._appends += 1
        if self._appends % PRUNE_EVERY_APPENDS == 0:
            self._prune()
        return seq

    def latest(self) -> int:
        with self._connect() as database:
            row = database.execute(
                "SELECT MAX(seq) FROM conversation_events"
            ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def covers(self, after: int) -> bool:
        """Return whether every event after `after` is still retained."""
        with self._connect() as database:
            row = database.execute(
                "SELECT MIN(seq) FROM conversation_events"
            ).fetchone()
        earliest = row[0] if row is not None else None
        return earliest is None or after >= int(earliest) - 1

    def after(self, seq: int, *, limit: int = 1_000) -> tuple[JournalEntry, ...]:
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT seq, payload FROM conversation_events
                WHERE seq > ? ORDER BY seq LIMIT ?
                """,
                (seq, limit),
            ).fetchall()
        return tuple(
            JournalEntry(int(row["seq"]), json.loads(str(row["payload"])))
            for row in rows
        )

    def _prune(self) -> None:
        with self._connect() as database:
            database.execute(
                "DELETE FROM conversation_events WHERE seq <= ?",
                (self.latest() - self._retained,),
            )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return connect(self.path)
