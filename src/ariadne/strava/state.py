"""Durable local storage for the short-lived Strava access token."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StravaTokens:
    """The current OAuth credentials; never return these to an MCP caller."""

    access_token: str
    refresh_token: str
    expires_at: int
    scope: str
    athlete_id: int | None


class StravaTokenState:
    """Store one athlete's OAuth credentials outside the TOML configuration."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS strava_tokens (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    athlete_id INTEGER
                )
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # A restrictive umask is normally enough; do not make the service
            # unusable on filesystems that do not expose POSIX permissions.
            pass

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database

    def load(self) -> StravaTokens | None:
        self.initialize()
        with self._connect() as database:
            row = database.execute(
                "SELECT access_token, refresh_token, expires_at, scope, athlete_id "
                "FROM strava_tokens WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return StravaTokens(
            access_token=str(row["access_token"]),
            refresh_token=str(row["refresh_token"]),
            expires_at=int(row["expires_at"]),
            scope=str(row["scope"]),
            athlete_id=int(row["athlete_id"])
            if row["athlete_id"] is not None
            else None,
        )

    def save(self, tokens: StravaTokens) -> None:
        self.initialize()
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO strava_tokens
                    (singleton, access_token, refresh_token, expires_at, scope,
                     athlete_id)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    scope = excluded.scope,
                    athlete_id = excluded.athlete_id
                """,
                (
                    tokens.access_token,
                    tokens.refresh_token,
                    tokens.expires_at,
                    tokens.scope,
                    tokens.athlete_id,
                ),
            )

    def clear(self) -> None:
        self.initialize()
        with self._connect() as database:
            database.execute("DELETE FROM strava_tokens WHERE singleton = 1")
