"""Push tokens registered by paired phones."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ..conversation.database import connect, prepare

PushEnvironment = Literal["sandbox", "production"]


@dataclass(frozen=True, slots=True)
class PushDevice:
    token: str
    environment: PushEnvironment


class DeviceStore:
    """Remember where push notifications for the owner may be delivered."""

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
                CREATE TABLE IF NOT EXISTS app_push_devices (
                    token TEXT PRIMARY KEY,
                    environment TEXT NOT NULL
                        CHECK(environment IN ('sandbox','production')),
                    registered_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def register(self, token: str, environment: PushEnvironment) -> PushDevice:
        now = time.time()
        normalized = token.lower()
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO app_push_devices (
                    token, environment, registered_at, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                    environment = excluded.environment,
                    updated_at = excluded.updated_at
                """,
                (normalized, environment, now, now),
            )
        return PushDevice(normalized, environment)

    def remove(self, token: str) -> None:
        with self._connect() as database:
            database.execute(
                "DELETE FROM app_push_devices WHERE token = ?", (token.lower(),)
            )

    def all(self) -> tuple[PushDevice, ...]:
        with self._connect() as database:
            rows = database.execute(
                "SELECT token, environment FROM app_push_devices ORDER BY registered_at"
            ).fetchall()
        return tuple(
            PushDevice(str(row["token"]), cast(PushEnvironment, row["environment"]))
            for row in rows
        )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return connect(self.path)
