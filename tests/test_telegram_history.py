import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from ariadne.telegram.history import (
    TelegramHistoryMessage,
    TelegramMessageSource,
    TelegramMessageStore,
    TelegramSpeaker,
)
from ariadne.telegram.questions import TelegramQuestionStore

START = datetime(2026, 8, 29, 9, tzinfo=UTC)


def message(
    message_id: int,
    minutes: int,
    text: str,
    *,
    chat_id: int = 7,
    speaker: TelegramSpeaker = "human",
    source: TelegramMessageSource = "telegram",
) -> TelegramHistoryMessage:
    return TelegramHistoryMessage(
        chat_id=chat_id,
        message_id=message_id,
        sent_at=START + timedelta(minutes=minutes),
        speaker=speaker,
        source=source,
        content_type="text",
        text=text,
    )


def test_history_uses_the_existing_private_telegram_database(tmp_path: Path) -> None:
    path = tmp_path / "private" / "telegram.sqlite3"
    TelegramQuestionStore(path).initialize()
    store = TelegramMessageStore(path)

    store.record(message(1, 0, "Hello"))

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert {"telegram_questions", "telegram_messages"}.issubset(tables)
    assert "telegram_messages_by_chat_and_time" in indexes


def test_record_is_idempotent_and_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "telegram.sqlite3"
    first_process = TelegramMessageStore(path)
    first_process.record(message(4, 0, "Draft wording"))
    first_process.record(message(4, 1, "Settled wording", speaker="iris"))

    page = TelegramMessageStore(path).read(7, since=START)

    assert page.total == 1
    assert len(page.messages) == 1
    assert page.messages[0].text == "Settled wording"
    assert page.messages[0].speaker == "iris"
    assert page.messages[0].sent_at == START + timedelta(minutes=1)


def test_read_filters_time_speaker_source_text_and_chat(tmp_path: Path) -> None:
    store = TelegramMessageStore(tmp_path / "telegram.sqlite3")
    for item in (
        message(1, 0, "Packing still needs doing"),
        message(2, 1, "I packed EVERYTHING", source="telegram"),
        message(3, 2, "Great — bib sorted", speaker="iris", source="wakeup"),
        message(4, 3, "Train reminder", speaker="iris", source="mail"),
        message(5, 4, "Other chat", chat_id=8),
    ):
        store.record(item)

    page = store.read(
        7,
        since=START + timedelta(seconds=30),
        before=START + timedelta(minutes=3),
        speakers=("human",),
        sources=("telegram",),
        query="packed everything",
    )

    assert [item.message_id for item in page.messages] == [2]
    assert page.total == 1
    assert page.earliest_available_at == START


def test_read_keeps_the_newest_limit_then_returns_it_chronologically(
    tmp_path: Path,
) -> None:
    store = TelegramMessageStore(tmp_path / "telegram.sqlite3")
    for number in range(1, 6):
        store.record(message(number, number, f"Message {number}"))

    page = store.read(7, since=START, limit=3)

    assert [item.message_id for item in page.messages] == [3, 4, 5]
    assert page.total == 5
    assert page.truncated is True
    assert page.earliest_available_at == START + timedelta(minutes=1)


def test_empty_history_reports_no_earliest_timestamp(tmp_path: Path) -> None:
    page = TelegramMessageStore(tmp_path / "telegram.sqlite3").read(7, since=START)

    assert page.messages == ()
    assert page.total == 0
    assert page.truncated is False
    assert page.earliest_available_at is None


@pytest.mark.parametrize(
    ("kwargs", "message_text"),
    [
        ({"since": datetime(2026, 8, 29)}, "since must include"),
        (
            {
                "since": START,
                "before": datetime(2026, 8, 30),
            },
            "before must include",
        ),
        (
            {"since": START, "before": START},
            "before must be later",
        ),
        ({"since": START, "limit": 0}, "between 1 and 100"),
        ({"since": START, "limit": 101}, "between 1 and 100"),
        (
            {
                "since": START,
                "speakers": (cast(TelegramSpeaker, "system"),),
            },
            "Unknown Telegram message speaker",
        ),
        (
            {
                "since": START,
                "sources": (cast(TelegramMessageSource, "calendar"),),
            },
            "Unknown Telegram message source",
        ),
    ],
)
def test_read_rejects_ambiguous_or_unbounded_inputs(
    tmp_path: Path, kwargs: dict[str, object], message_text: str
) -> None:
    store = TelegramMessageStore(tmp_path / "telegram.sqlite3")

    with pytest.raises(ValueError, match=message_text):
        store.read(7, **kwargs)  # type: ignore[arg-type]


def test_record_rejects_invisible_text_and_timezone_free_time(tmp_path: Path) -> None:
    store = TelegramMessageStore(tmp_path / "telegram.sqlite3")

    with pytest.raises(ValueError, match="visible text"):
        store.record(message(1, 0, "  "))
    with pytest.raises(ValueError, match="timezone"):
        store.record(
            TelegramHistoryMessage(
                chat_id=7,
                message_id=2,
                sent_at=datetime(2026, 8, 29),
                speaker="human",
                source="telegram",
                content_type="text",
                text="Hello",
            )
        )
