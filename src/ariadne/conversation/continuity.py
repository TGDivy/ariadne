"""Durable Codex-thread identity for the one shared conversation."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..codex import PersistedConversationThread
from .database import connect, prepare


class ConversationThreadStore:
    """Persist one versioned thread reference per shared profile."""

    def __init__(self, path: Path, *, profile_name: str) -> None:
        if not profile_name.strip():
            raise ValueError("A conversation thread needs a profile name.")
        self.path = path
        self._profile_name = profile_name
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        prepare(self.path)
        with connect(self.path) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_threads (
                    profile_name TEXT PRIMARY KEY,
                    state_version INTEGER NOT NULL,
                    thread_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def load(self) -> PersistedConversationThread | None:
        with self._connect() as database:
            row = database.execute(
                """
                SELECT state_version, thread_id FROM conversation_threads
                WHERE profile_name = ?
                """,
                (self._profile_name,),
            ).fetchone()
        if row is None:
            return None
        return PersistedConversationThread(
            version=int(row["state_version"]), thread_id=str(row["thread_id"])
        )

    def save(self, state: PersistedConversationThread) -> None:
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO conversation_threads (
                    profile_name, state_version, thread_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(profile_name) DO UPDATE SET
                    state_version = excluded.state_version,
                    thread_id = excluded.thread_id,
                    updated_at = excluded.updated_at
                """,
                (self._profile_name, state.version, state.thread_id, time.time()),
            )

    def clear(self) -> None:
        with self._connect() as database:
            database.execute(
                "DELETE FROM conversation_threads WHERE profile_name = ?",
                (self._profile_name,),
            )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return connect(self.path)
