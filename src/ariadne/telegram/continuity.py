"""Durable Codex-thread identity for the private Telegram conversation."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..codex import PersistedConversationThread


class TelegramConversationThreadStore:
    """Persist one versioned thread reference in Telegram's private state."""

    def __init__(
        self,
        path: Path,
        *,
        owner_id: int,
        chat_id: int,
        profile_name: str,
    ) -> None:
        if not profile_name.strip():
            raise ValueError("A conversation thread needs a profile name.")
        self.path = path
        self._owner_id = owner_id
        self._chat_id = chat_id
        self._profile_name = profile_name
        self._initialized = False

    def initialize(self) -> None:
        """Create the private table without disturbing other Telegram state."""
        if self._initialized:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connect_unchecked() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_conversation_threads (
                    owner_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    profile_name TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    thread_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (owner_id, chat_id, profile_name)
                )
                """
            )
        self._initialized = True

    def load(self) -> PersistedConversationThread | None:
        """Load this owner/chat/profile's saved thread reference."""
        with self._connect() as database:
            row = database.execute(
                """
                SELECT state_version, thread_id
                FROM telegram_conversation_threads
                WHERE owner_id = ? AND chat_id = ? AND profile_name = ?
                """,
                (self._owner_id, self._chat_id, self._profile_name),
            ).fetchone()
        if row is None:
            return None
        return PersistedConversationThread(
            version=int(row["state_version"]),
            thread_id=str(row["thread_id"]),
        )

    def save(self, state: PersistedConversationThread) -> None:
        """Atomically insert or replace this conversation's reference."""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                INSERT INTO telegram_conversation_threads (
                    owner_id, chat_id, profile_name,
                    state_version, thread_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, chat_id, profile_name) DO UPDATE SET
                    state_version = excluded.state_version,
                    thread_id = excluded.thread_id,
                    updated_at = excluded.updated_at
                """,
                (
                    self._owner_id,
                    self._chat_id,
                    self._profile_name,
                    state.version,
                    state.thread_id,
                    time.time(),
                ),
            )

    def clear(self) -> None:
        """Atomically forget only this conversation's reference."""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                DELETE FROM telegram_conversation_threads
                WHERE owner_id = ? AND chat_id = ? AND profile_name = ?
                """,
                (self._owner_id, self._chat_id, self._profile_name),
            )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect_unchecked()

    def _connect_unchecked(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=30)
        database.row_factory = sqlite3.Row
        return database
