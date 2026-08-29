"""Durable recent Telegram messages shared by every Ariadne surface."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

TelegramSpeaker = Literal["human", "iris"]
TelegramMessageSource = Literal["telegram", "mail", "wakeup"]
TelegramContentType = Literal["text", "photo", "document"]


def telegram_message_time(message: object) -> datetime:
    """Use Telegram's stable bubble time, with observation time for test doubles."""
    value = getattr(message, "date", None)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TelegramHistoryMessage:
    """One permanent message visible in the private Telegram conversation."""

    chat_id: int
    message_id: int
    sent_at: datetime
    speaker: TelegramSpeaker
    source: TelegramMessageSource
    content_type: TelegramContentType
    text: str
    reply_to_message_id: int | None = None

    def public_payload(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "sent_at": self.sent_at.astimezone(UTC).isoformat(),
            "speaker": self.speaker,
            "source": self.source,
            "content_type": self.content_type,
            "text": self.text,
            "reply_to_message_id": self.reply_to_message_id,
        }


@dataclass(frozen=True, slots=True)
class TelegramHistoryPage:
    """A bounded chronological selection from locally observed messages."""

    messages: tuple[TelegramHistoryMessage, ...]
    total: int
    earliest_available_at: datetime | None

    @property
    def truncated(self) -> bool:
        return self.total > len(self.messages)


class TelegramMessageStore:
    """Store permanent Telegram messages in Ariadne's existing private state."""

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
                CREATE TABLE IF NOT EXISTS telegram_messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sent_at REAL NOT NULL,
                    speaker TEXT NOT NULL CHECK(speaker IN ('human','iris')),
                    source TEXT NOT NULL
                        CHECK(source IN ('telegram','mail','wakeup')),
                    content_type TEXT NOT NULL
                        CHECK(content_type IN ('text','photo','document')),
                    text TEXT NOT NULL,
                    reply_to_message_id INTEGER,
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            database.execute(
                """
                CREATE INDEX IF NOT EXISTS telegram_messages_by_chat_and_time
                ON telegram_messages(chat_id, sent_at, message_id)
                """
            )
        self._initialized = True

    def record(self, message: TelegramHistoryMessage) -> None:
        """Insert or replace one Telegram message without creating duplicates."""
        if message.sent_at.tzinfo is None or message.sent_at.utcoffset() is None:
            raise ValueError("Telegram message times must include a timezone.")
        text = message.text.strip()
        if not text:
            raise ValueError("A Telegram history message needs visible text.")
        with self._connect() as database:
            database.execute(
                """
                INSERT INTO telegram_messages (
                    chat_id, message_id, sent_at, speaker, source,
                    content_type, text, reply_to_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    sent_at = excluded.sent_at,
                    speaker = excluded.speaker,
                    source = excluded.source,
                    content_type = excluded.content_type,
                    text = excluded.text,
                    reply_to_message_id = excluded.reply_to_message_id
                """,
                (
                    message.chat_id,
                    message.message_id,
                    message.sent_at.astimezone(UTC).timestamp(),
                    message.speaker,
                    message.source,
                    message.content_type,
                    text,
                    message.reply_to_message_id,
                ),
            )

    def read(
        self,
        chat_id: int,
        *,
        since: datetime,
        before: datetime | None = None,
        speakers: Sequence[TelegramSpeaker] = (),
        sources: Sequence[TelegramMessageSource] = (),
        query: str | None = None,
        limit: int = 50,
    ) -> TelegramHistoryPage:
        """Read the newest bounded match set and return it chronologically."""
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("since must include a timezone offset.")
        if before is not None:
            if before.tzinfo is None or before.utcoffset() is None:
                raise ValueError("before must include a timezone offset.")
            if before <= since:
                raise ValueError("before must be later than since.")
        if not 1 <= limit <= 100:
            raise ValueError("Telegram history limit must be between 1 and 100.")
        invalid_speakers = set(speakers) - {"human", "iris"}
        if invalid_speakers:
            raise ValueError("Unknown Telegram message speaker.")
        invalid_sources = set(sources) - {"telegram", "mail", "wakeup"}
        if invalid_sources:
            raise ValueError("Unknown Telegram message source.")

        clauses = ["chat_id = ?", "sent_at >= ?"]
        values: list[object] = [chat_id, since.astimezone(UTC).timestamp()]
        if before is not None:
            clauses.append("sent_at < ?")
            values.append(before.astimezone(UTC).timestamp())
        if speakers:
            clauses.append("speaker IN (" + ",".join("?" for _ in speakers) + ")")
            values.extend(speakers)
        if sources:
            clauses.append("source IN (" + ",".join("?" for _ in sources) + ")")
            values.extend(sources)
        normalized_query = query.strip().casefold() if query is not None else ""
        if normalized_query:
            clauses.append("instr(CASEFOLD(text), ?) > 0")
            values.append(normalized_query)
        where = " AND ".join(clauses)

        with self._connect() as database:
            earliest = database.execute(
                "SELECT MIN(sent_at) FROM telegram_messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0]
            total = int(
                database.execute(
                    f"SELECT COUNT(*) FROM telegram_messages WHERE {where}", values
                ).fetchone()[0]
            )
            rows = database.execute(
                f"""
                SELECT * FROM telegram_messages
                WHERE {where}
                ORDER BY sent_at DESC, message_id DESC
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return TelegramHistoryPage(
            messages=tuple(_message(row) for row in reversed(rows)),
            total=total,
            earliest_available_at=(
                datetime.fromtimestamp(float(earliest), UTC)
                if earliest is not None
                else None
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect_unchecked()

    def _connect_unchecked(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        database.create_function(
            "CASEFOLD", 1, lambda value: str(value).casefold(), deterministic=True
        )
        return database


def _message(row: sqlite3.Row) -> TelegramHistoryMessage:
    return TelegramHistoryMessage(
        chat_id=int(row["chat_id"]),
        message_id=int(row["message_id"]),
        sent_at=datetime.fromtimestamp(float(row["sent_at"]), UTC),
        speaker=cast(TelegramSpeaker, row["speaker"]),
        source=cast(TelegramMessageSource, row["source"]),
        content_type=cast(TelegramContentType, row["content_type"]),
        text=str(row["text"]),
        reply_to_message_id=(
            int(row["reply_to_message_id"])
            if row["reply_to_message_id"] is not None
            else None
        ),
    )
