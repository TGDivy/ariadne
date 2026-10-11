"""Private files attached to conversation messages."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import cast

from .database import connect, prepare
from .models import Attachment, AttachmentKind

SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
DEFAULT_ATTACHMENT_ROOT = Path("~/.ariadne/attachments")


class AttachmentError(ValueError):
    """An attachment could not be accepted safely."""


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """Return a sender's filename without any directory component."""
    candidate = Path(name.replace("\\", "/")).name.strip()
    return candidate if candidate not in {"", ".", ".."} else fallback


class AttachmentStore:
    """Reserve, record, and resolve attachment files under one private root."""

    def __init__(self, state: Path, root: Path) -> None:
        self.state = state
        self.root = root
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        prepare(self.state)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with connect(self.state) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_attachments (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('image','document','file')),
                    filename TEXT NOT NULL,
                    mime_type TEXT,
                    size_bytes INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def reserve(self, filename: str) -> Path:
        """Reserve a readable path for a new file under today's folder.

        The empty file is the reservation: checking for existence and creating
        the file later would let concurrent uploads pick the same path.
        """
        self.initialize()
        directory = self.root / date.today().isoformat()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        candidate = directory / safe_filename(filename)
        stem, suffix = candidate.stem, candidate.suffix
        attempt = 2
        while True:
            try:
                candidate.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                candidate = directory / f"{stem}-{attempt}{suffix}"
                attempt += 1
            else:
                return candidate

    def register(
        self,
        path: Path,
        *,
        filename: str,
        mime_type: str | None,
        kind: AttachmentKind,
    ) -> Attachment:
        """Record a completely written file and return its opaque attachment."""
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or not resolved.is_file():
            raise AttachmentError("Attachments must be stored in the attachment root.")
        attachment = Attachment(
            id=f"att_{secrets.token_urlsafe(12)}",
            kind=kind,
            filename=safe_filename(filename),
            mime_type=mime_type,
            size_bytes=resolved.stat().st_size,
            path=resolved,
        )
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO conversation_attachments (
                    id, kind, filename, mime_type, size_bytes, path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment.id,
                    attachment.kind,
                    attachment.filename,
                    attachment.mime_type,
                    attachment.size_bytes,
                    str(attachment.path),
                    time.time(),
                ),
            )
        return attachment

    def copy_in(self, source: Path, *, kind: AttachmentKind = "file") -> Attachment:
        """Snapshot an existing file so later edits cannot change what was sent."""
        target = self.reserve(source.name)
        try:
            shutil.copyfile(source, target)
        except OSError as error:
            target.unlink(missing_ok=True)
            raise AttachmentError(
                f"The file could not be copied: {source.name}"
            ) from error
        return self.register(target, filename=source.name, mime_type=None, kind=kind)

    def get(self, identifier: str) -> Attachment | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM conversation_attachments WHERE id = ?",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        return Attachment(
            id=str(row["id"]),
            kind=cast(AttachmentKind, row["kind"]),
            filename=str(row["filename"]),
            mime_type=cast(str | None, row["mime_type"]),
            size_bytes=int(row["size_bytes"]),
            path=Path(str(row["path"])),
        )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return connect(self.state)
