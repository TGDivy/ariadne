"""Files Iris shares into the conversation from inside a Codex turn."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .database import connect, prepare

MAX_SHARED_FILES = 10
MAX_SHARED_FILE_BYTES = 100 * 1024 * 1024
MAX_CAPTION_LENGTH = 2_000


class ShareError(ValueError):
    """A requested share cannot be delivered safely."""


@dataclass(frozen=True, slots=True)
class PendingShare:
    """Validated files waiting for the conversation core to deliver them."""

    id: str
    paths: tuple[Path, ...]
    caption: str | None


def validate_share(raw_paths: list[str], caption: str | None) -> tuple[Path, ...]:
    """Resolve readable regular files under the owner's home directory."""
    if not raw_paths:
        raise ShareError("At least one file is required.")
    if len(raw_paths) > MAX_SHARED_FILES:
        raise ShareError(f"Share at most {MAX_SHARED_FILES} files at once.")
    if caption is not None and len(caption) > MAX_CAPTION_LENGTH:
        raise ShareError(f"A caption can be at most {MAX_CAPTION_LENGTH} characters.")
    home = Path.home().resolve()
    paths: list[Path] = []
    for raw_path in raw_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_relative_to(home):
            raise ShareError(f"File is outside the allowed home directory: {raw_path}")
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ShareError(f"File is not a readable regular file: {raw_path}")
        if path.stat().st_size > MAX_SHARED_FILE_BYTES:
            raise ShareError(f"File is larger than 100 MB: {raw_path}")
        paths.append(path)
    return tuple(paths)


class SharedFiles:
    """An outbox written by the MCP process and drained by the core."""

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
                CREATE TABLE IF NOT EXISTS conversation_shares (
                    id TEXT PRIMARY KEY,
                    paths TEXT NOT NULL,
                    caption TEXT,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','delivered','failed')),
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def stage(self, paths: tuple[Path, ...], caption: str | None) -> PendingShare:
        share = PendingShare(
            id=f"share_{secrets.token_urlsafe(9)}",
            paths=paths,
            caption=caption.strip() or None if caption is not None else None,
        )
        now = time.time()
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO conversation_shares (
                    id, paths, caption, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (
                    share.id,
                    json.dumps([str(path) for path in paths]),
                    share.caption,
                    now,
                    now,
                ),
            )
        return share

    def pending(self) -> tuple[PendingShare, ...]:
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT * FROM conversation_shares
                WHERE status = 'pending' ORDER BY created_at, id
                """
            ).fetchall()
        return tuple(
            PendingShare(
                id=str(row["id"]),
                paths=tuple(Path(path) for path in json.loads(str(row["paths"]))),
                caption=str(row["caption"]) if row["caption"] is not None else None,
            )
            for row in rows
        )

    def finish(self, identifier: str, *, error: str | None = None) -> None:
        with self._connect() as database:
            database.execute(
                """
                UPDATE conversation_shares
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                ("failed" if error else "delivered", error, time.time(), identifier),
            )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return connect(self.path)
