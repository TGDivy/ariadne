"""The durable message log: the conversation's single commit point."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .database import connect, prepare
from .models import (
    ORIGINS,
    SPEAKERS,
    Attachment,
    AttachmentKind,
    ConversationMessage,
    HistoryPage,
    MessageDraft,
    MessageKind,
    MessageOrigin,
    Speaker,
    SpeechPhase,
)

MAX_REACTION_LENGTH = 128
MAX_HISTORY_LIMIT = 100


class ConversationLog:
    """Persist every permanent human and Iris message, in commit order."""

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
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_message_id TEXT UNIQUE,
                    sent_at REAL NOT NULL,
                    speaker TEXT NOT NULL CHECK(speaker IN ('human','iris')),
                    origin TEXT NOT NULL CHECK(origin IN ('direct','proactive')),
                    kind TEXT NOT NULL
                        CHECK(kind IN ('text','voice','question','answer')),
                    text TEXT NOT NULL,
                    reply_to_id INTEGER,
                    turn_id TEXT,
                    phase TEXT CHECK(phase IN ('commentary','final') OR phase IS NULL),
                    stopped INTEGER NOT NULL DEFAULT 0,
                    question_id TEXT,
                    attachments TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            database.execute(
                """
                CREATE INDEX IF NOT EXISTS conversation_messages_by_time
                ON conversation_messages(sent_at, id)
                """
            )
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_reactions (
                    message_id INTEGER PRIMARY KEY
                        REFERENCES conversation_messages(id) ON DELETE CASCADE,
                    reactions TEXT NOT NULL,
                    reacted_at REAL NOT NULL
                )
                """
            )
        self._initialized = True

    def append(self, draft: MessageDraft) -> ConversationMessage:
        """Commit one message, returning the earlier one for a repeated client id."""
        if draft.sent_at.tzinfo is None or draft.sent_at.utcoffset() is None:
            raise ValueError("Message times must include a timezone.")
        text = draft.text.strip()
        if not text and not draft.attachments:
            raise ValueError("A message needs text or an attachment.")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if draft.client_message_id is not None:
                existing = database.execute(
                    "SELECT id FROM conversation_messages WHERE client_message_id = ?",
                    (draft.client_message_id,),
                ).fetchone()
                if existing is not None:
                    return self._select(database, int(existing["id"]))
            cursor = database.execute(
                """
                INSERT INTO conversation_messages (
                    client_message_id, sent_at, speaker, origin, kind, text,
                    reply_to_id, turn_id, phase, stopped, question_id, attachments
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.client_message_id,
                    draft.sent_at.astimezone(UTC).timestamp(),
                    draft.speaker,
                    draft.origin,
                    draft.kind,
                    text,
                    draft.reply_to_id,
                    draft.turn_id,
                    draft.phase,
                    int(draft.stopped),
                    draft.question_id,
                    json.dumps(
                        [_attachment_record(item) for item in draft.attachments]
                    ),
                ),
            )
            assert cursor.lastrowid is not None
            return self._select(database, cursor.lastrowid)

    def get(self, message_id: int) -> ConversationMessage | None:
        with self._connect() as database:
            row = database.execute(
                f"{_SELECT} WHERE m.id = ?",
                (message_id,),
            ).fetchone()
        return _message(row) if row is not None else None

    def by_client_id(self, client_message_id: str) -> ConversationMessage | None:
        with self._connect() as database:
            row = database.execute(
                f"{_SELECT} WHERE m.client_message_id = ?",
                (client_message_id,),
            ).fetchone()
        return _message(row) if row is not None else None

    def latest_id(self) -> int | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT MAX(id) FROM conversation_messages"
            ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else None

    def page(
        self, *, before: int | None = None, limit: int = 50
    ) -> tuple[tuple[ConversationMessage, ...], bool]:
        """Return the newest messages older than `before`, oldest first."""
        if not 1 <= limit <= 200:
            raise ValueError("A message page holds between 1 and 200 messages.")
        clause, values = (
            ("WHERE m.id < ?", (before,)) if before is not None else ("", ())
        )
        with self._connect() as database:
            rows = database.execute(
                f"{_SELECT} {clause} ORDER BY m.id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        return tuple(_message(row) for row in reversed(rows[:limit])), has_more

    def read(
        self,
        *,
        since: datetime,
        before: datetime | None = None,
        speakers: Sequence[Speaker] = (),
        origins: Sequence[MessageOrigin] = (),
        query: str | None = None,
        limit: int = 50,
    ) -> HistoryPage:
        """Read the newest bounded match set and return it chronologically."""
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must include a timezone offset.")
        if before is not None:
            if before.tzinfo is None or before.utcoffset() is None:
                raise ValueError("before must include a timezone offset.")
            if before <= since:
                raise ValueError("before must be later than since.")
        if not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ValueError(
                f"History limit must be between 1 and {MAX_HISTORY_LIMIT}."
            )
        if set(speakers) - set(SPEAKERS):
            raise ValueError("Unknown message speaker.")
        if set(origins) - set(ORIGINS):
            raise ValueError("Unknown message origin.")

        clauses = ["m.sent_at >= ?"]
        values: list[object] = [since.astimezone(UTC).timestamp()]
        if before is not None:
            clauses.append("m.sent_at < ?")
            values.append(before.astimezone(UTC).timestamp())
        if speakers:
            clauses.append("m.speaker IN (" + ",".join("?" for _ in speakers) + ")")
            values.extend(speakers)
        if origins:
            clauses.append("m.origin IN (" + ",".join("?" for _ in origins) + ")")
            values.extend(origins)
        normalized_query = query.strip().casefold() if query is not None else ""
        if normalized_query:
            clauses.append("instr(CASEFOLD(m.text), ?) > 0")
            values.append(normalized_query)
        where = " AND ".join(clauses)

        with self._connect() as database:
            earliest = database.execute(
                "SELECT MIN(sent_at) FROM conversation_messages"
            ).fetchone()[0]
            total = int(
                database.execute(
                    f"SELECT COUNT(*) FROM conversation_messages AS m WHERE {where}",
                    values,
                ).fetchone()[0]
            )
            rows = database.execute(
                f"{_SELECT} WHERE {where} ORDER BY m.sent_at DESC, m.id DESC LIMIT ?",
                (*values, limit),
            ).fetchall()
        return HistoryPage(
            messages=tuple(_message(row) for row in reversed(rows)),
            total=total,
            earliest_available_at=(
                datetime.fromtimestamp(float(earliest), UTC)
                if earliest is not None
                else None
            ),
        )

    def set_reactions(self, message_id: int, reactions: Sequence[str]) -> bool:
        """Replace the owner's current reactions on one of Iris's messages."""
        normalized = tuple(dict.fromkeys(reaction.strip() for reaction in reactions))
        if any(
            not reaction or len(reaction) > MAX_REACTION_LENGTH
            for reaction in normalized
        ):
            raise ValueError("Reactions must be short visible values.")
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT speaker FROM conversation_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None or row["speaker"] != "iris":
                return False
            if normalized:
                database.execute(
                    """
                    INSERT INTO conversation_reactions (
                        message_id, reactions, reacted_at
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        reactions = excluded.reactions,
                        reacted_at = excluded.reacted_at
                    """,
                    (
                        message_id,
                        json.dumps(normalized, ensure_ascii=False),
                        time.time(),
                    ),
                )
            else:
                database.execute(
                    "DELETE FROM conversation_reactions WHERE message_id = ?",
                    (message_id,),
                )
        return True

    def _select(
        self, database: sqlite3.Connection, message_id: int
    ) -> ConversationMessage:
        row = database.execute(f"{_SELECT} WHERE m.id = ?", (message_id,)).fetchone()
        assert row is not None
        return _message(row)

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        database = connect(self.path)
        database.create_function(
            "CASEFOLD", 1, lambda value: str(value).casefold(), deterministic=True
        )
        return database


_SELECT = """
    SELECT m.*, r.reactions AS reactions_json
    FROM conversation_messages AS m
    LEFT JOIN conversation_reactions AS r ON r.message_id = m.id
"""


def _attachment_record(attachment: Attachment) -> dict[str, object]:
    return {
        "id": attachment.id,
        "kind": attachment.kind,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "path": str(attachment.path),
    }


def _attachment(record: dict[str, Any]) -> Attachment:
    return Attachment(
        id=str(record["id"]),
        kind=cast(AttachmentKind, record["kind"]),
        filename=str(record["filename"]),
        mime_type=(
            str(record["mime_type"]) if record.get("mime_type") is not None else None
        ),
        size_bytes=int(record["size_bytes"]),
        path=Path(str(record["path"])),
    )


def _message(row: sqlite3.Row) -> ConversationMessage:
    reactions_json = row["reactions_json"]
    return ConversationMessage(
        id=int(row["id"]),
        client_message_id=cast(str | None, row["client_message_id"]),
        sent_at=datetime.fromtimestamp(float(row["sent_at"]), UTC),
        speaker=cast(Speaker, row["speaker"]),
        origin=cast(MessageOrigin, row["origin"]),
        kind=cast(MessageKind, row["kind"]),
        text=str(row["text"]),
        reply_to_id=(
            int(row["reply_to_id"]) if row["reply_to_id"] is not None else None
        ),
        turn_id=cast(str | None, row["turn_id"]),
        phase=cast(SpeechPhase | None, row["phase"]),
        stopped=bool(row["stopped"]),
        question_id=cast(str | None, row["question_id"]),
        attachments=tuple(
            _attachment(record) for record in json.loads(str(row["attachments"]))
        ),
        reactions=(
            tuple(cast(list[str], json.loads(str(reactions_json))))
            if reactions_json is not None
            else ()
        ),
    )
