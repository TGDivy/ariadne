import sqlite3
from pathlib import Path

from ariadne.codex import PersistedConversationThread
from ariadne.telegram.continuity import TelegramConversationThreadStore
from ariadne.telegram.history import TelegramMessageStore
from ariadne.telegram.questions import TelegramQuestionStore


def store(
    path: Path,
    *,
    owner_id: int = 7,
    chat_id: int = 7,
    profile_name: str = "telegram",
) -> TelegramConversationThreadStore:
    return TelegramConversationThreadStore(
        path,
        owner_id=owner_id,
        chat_id=chat_id,
        profile_name=profile_name,
    )


def test_thread_state_shares_the_private_telegram_database(tmp_path: Path) -> None:
    path = tmp_path / "private" / "telegram.sqlite3"
    TelegramQuestionStore(path).initialize()
    TelegramMessageStore(path).initialize()
    state = store(path)

    state.save(PersistedConversationThread(1, "thread-one"))

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as database:
        tables = {
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "telegram_questions",
        "telegram_messages",
        "telegram_conversation_threads",
    }.issubset(tables)


def test_thread_state_is_restart_safe_replaceable_and_clearable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "telegram.sqlite3"
    first = store(path)
    first.save(PersistedConversationThread(1, "first"))
    first.save(PersistedConversationThread(1, "second"))

    restarted = store(path)

    assert restarted.load() == PersistedConversationThread(1, "second")

    restarted.clear()

    assert store(path).load() is None


def test_thread_state_is_scoped_by_owner_chat_and_profile(tmp_path: Path) -> None:
    path = tmp_path / "telegram.sqlite3"
    expected = {
        (7, 7, "telegram"): "owner-seven",
        (8, 7, "telegram"): "owner-eight",
        (7, 8, "telegram"): "chat-eight",
        (7, 7, "other-profile"): "other-profile",
    }
    for (owner_id, chat_id, profile_name), thread_id in expected.items():
        store(
            path,
            owner_id=owner_id,
            chat_id=chat_id,
            profile_name=profile_name,
        ).save(PersistedConversationThread(1, thread_id))

    for (owner_id, chat_id, profile_name), thread_id in expected.items():
        assert store(
            path,
            owner_id=owner_id,
            chat_id=chat_id,
            profile_name=profile_name,
        ).load() == PersistedConversationThread(1, thread_id)

    store(path, owner_id=8).clear()

    assert store(path, owner_id=8).load() is None
    assert store(path).load() == PersistedConversationThread(1, "owner-seven")


def test_store_preserves_unknown_version_for_explicit_compatibility_handling(
    tmp_path: Path,
) -> None:
    state = store(tmp_path / "telegram.sqlite3")
    state.save(PersistedConversationThread(99, "future-thread"))

    assert state.load() == PersistedConversationThread(99, "future-thread")
