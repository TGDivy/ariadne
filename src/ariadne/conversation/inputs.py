"""Accepted human input that has not reached Codex yet survives a restart."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .database import connect, prepare
from .models import PendingInput


class PendingInputs:
    """A FIFO of inputs waiting to steer the active turn or start the next one."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        prepare(self.path)
        with connect(self.path) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_inputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    image_paths TEXT NOT NULL,
                    force_next_turn INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def add(
        self,
        *,
        message_id: int,
        prompt: str,
        image_paths: tuple[Path, ...],
        force_next_turn: bool = False,
    ) -> PendingInput:
        with self._connect() as database:
            cursor = database.execute(
                """
                INSERT INTO conversation_inputs (
                    message_id, prompt, image_paths, force_next_turn, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    prompt,
                    json.dumps([str(path) for path in image_paths]),
                    int(force_next_turn),
                    time.time(),
                ),
            )
            assert cursor.lastrowid is not None
            identifier = cursor.lastrowid
        return PendingInput(
            id=identifier,
            message_id=message_id,
            prompt=prompt,
            image_paths=image_paths,
            force_next_turn=force_next_turn,
        )

    def first(self) -> PendingInput | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM conversation_inputs ORDER BY id LIMIT 1"
            ).fetchone()
        return _input(row) if row is not None else None

    def count(self) -> int:
        with self._connect() as database:
            row = database.execute(
                "SELECT COUNT(*) FROM conversation_inputs"
            ).fetchone()
        return int(row[0])

    def remove(self, identifier: int) -> None:
        with self._connect() as database:
            database.execute(
                "DELETE FROM conversation_inputs WHERE id = ?", (identifier,)
            )

    def require_next_turn(self, identifier: int) -> None:
        """Keep an input whose steering outcome is unknown out of the live turn."""
        with self._connect() as database:
            database.execute(
                "UPDATE conversation_inputs SET force_next_turn = 1 WHERE id = ?",
                (identifier,),
            )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return connect(self.path)


def _input(row: sqlite3.Row) -> PendingInput:
    return PendingInput(
        id=int(row["id"]),
        message_id=int(row["message_id"]),
        prompt=str(row["prompt"]),
        image_paths=tuple(Path(path) for path in json.loads(str(row["image_paths"]))),
        force_next_turn=bool(row["force_next_turn"]),
    )
