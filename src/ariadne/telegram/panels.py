"""Durable identity for Telegram's one ephemeral control panel."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TelegramControlPanel:
    chat_id: int
    message_id: int
    kind: str


class TelegramControlPanelStore:
    """Keep at most one restart-cleanable control panel per chat."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connect_unchecked() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_control_panels (
                    chat_id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def get(self, chat_id: int) -> TelegramControlPanel | None:
        with self._connect() as database:
            row = database.execute(
                """SELECT chat_id, message_id, kind
                FROM telegram_control_panels WHERE chat_id = ?""",
                (chat_id,),
            ).fetchone()
        return (
            TelegramControlPanel(
                chat_id=int(row["chat_id"]),
                message_id=int(row["message_id"]),
                kind=str(row["kind"]),
            )
            if row is not None
            else None
        )

    def set(self, panel: TelegramControlPanel) -> None:
        kind = panel.kind.strip()
        if not kind or len(kind) > 64:
            raise ValueError("A Telegram control panel needs a short kind.")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                INSERT INTO telegram_control_panels (
                    chat_id, message_id, kind, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    kind = excluded.kind,
                    updated_at = excluded.updated_at
                """,
                (panel.chat_id, panel.message_id, kind, time.time()),
            )

    def clear(self, chat_id: int, *, expected_message_id: int | None = None) -> bool:
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if expected_message_id is None:
                cursor = database.execute(
                    "DELETE FROM telegram_control_panels WHERE chat_id = ?",
                    (chat_id,),
                )
            else:
                cursor = database.execute(
                    """
                    DELETE FROM telegram_control_panels
                    WHERE chat_id = ? AND message_id = ?
                    """,
                    (chat_id, expected_message_id),
                )
        return cursor.rowcount == 1

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect_unchecked()

    def _connect_unchecked(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database
