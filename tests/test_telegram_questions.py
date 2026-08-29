from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from telegram import Bot
from telegram.error import BadRequest

from ariadne.telegram import questions as question_module
from ariadne.telegram.questions import (
    ActiveQuestionError,
    TelegramQuestionCard,
    TelegramQuestionStore,
    parse_question_callback,
    validate_question,
)


class FakeBot:
    def __init__(self, *, reject_rich: bool = False) -> None:
        self.reject_rich = reject_rich
        self.api_calls: list[tuple[str, dict[str, Any]]] = []

    async def do_api_request(
        self,
        endpoint: str,
        api_kwargs: dict[str, Any] | None = None,
        **_: object,
    ) -> SimpleNamespace:
        arguments = api_kwargs or {}
        if endpoint == "sendRichMessage" and self.reject_rich:
            raise BadRequest("Rich Messages unavailable")
        self.api_calls.append((endpoint, arguments))
        return SimpleNamespace(
            message_id=100 + len(self.api_calls),
            chat_id=arguments.get("chat_id", 7),
        )


def test_button_answers_are_validated_and_applied_once(tmp_path: Path) -> None:
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    question = store.create(7, "Where should I deploy?", ["Staging", "Production"])
    store.attach_message(question.question_id, 90)

    wrong_message = store.answer_choice(
        question.question_id, chat_id=7, message_id=91, choice_index=1
    )
    accepted = store.answer_choice(
        question.question_id, chat_id=7, message_id=90, choice_index=1
    )
    duplicate = store.answer_choice(
        question.question_id, chat_id=7, message_id=90, choice_index=1
    )

    assert wrong_message.outcome == "stale"
    assert accepted.outcome == "accepted"
    assert accepted.question is not None
    assert accepted.question.answer == "Production"
    assert accepted.question.answer_source == "button"
    assert duplicate.outcome == "already_answered"


def test_any_typed_text_can_answer_the_active_question(tmp_path: Path) -> None:
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    question = store.create(7, "Which environment?", ["Staging", "Production"])

    answered = store.answer_text(7, "Production, but use a 10% canary")

    assert answered is not None
    assert answered.question_id == question.question_id
    assert answered.answer == "Production, but use a 10% canary"
    assert answered.answer_source == "text"
    assert store.answer_text(7, "A second answer") is None


def test_only_one_question_can_wait_in_a_chat(tmp_path: Path) -> None:
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    first = store.create(7, "First?", ["A", "B"])

    with pytest.raises(ActiveQuestionError):
        store.create(7, "Second?", ["C", "D"])

    store.cancel(first.question_id)
    second = store.create(7, "Second?", ["C", "D"])
    assert second.status == "pending"


def test_expired_and_cancelled_questions_cannot_be_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    monkeypatch.setattr(question_module.time, "time", lambda: clock[0])
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    expired = store.create(7, "Expired?", ["A", "B"], ttl_seconds=5)
    store.attach_message(expired.question_id, 90)
    clock[0] = 106.0

    refreshed = store.get(expired.question_id)
    selection = store.answer_choice(
        expired.question_id, chat_id=7, message_id=90, choice_index=0
    )

    assert refreshed is not None
    assert refreshed.status == "expired"
    assert selection.outcome == "inactive"


def test_restart_recovery_cancels_orphaned_questions(tmp_path: Path) -> None:
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    question = store.create(7, "Still there?", ["Yes", "No"])
    store.attach_message(question.question_id, 90)

    recovered = TelegramQuestionStore(store.path).cancel_pending(7)

    assert len(recovered) == 1
    assert recovered[0].status == "cancelled"
    assert store.pending(7) is None


async def test_question_card_uses_embedded_rich_buttons_and_disables_them(
    tmp_path: Path,
) -> None:
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    question = store.create(7, "**Choose** an environment", ["Staging", "Production"])
    fake = FakeBot()
    card = TelegramQuestionCard(cast(Bot, fake))

    message = await card.send(question)
    attached = store.attach_message(question.question_id, message.message_id)
    assert attached is not None
    selection = store.answer_choice(
        question.question_id,
        chat_id=7,
        message_id=message.message_id,
        choice_index=1,
    )
    assert selection.question is not None
    await card.settle(selection.question)

    assert fake.api_calls[0][0] == "sendRichMessage"
    sent_markdown = fake.api_calls[0][1]["rich_message"]["markdown"]
    assert '<tg-button type="callback_data" style="primary"' in sent_markdown
    assert sent_markdown.count("<tg-button-row") == 1
    assert fake.api_calls[1][0] == "editMessageText"
    final_markdown = fake.api_calls[1][1]["rich_message"]["markdown"]
    assert '<tg-button type="disabled" style="success">✓ Production' in final_markdown


async def test_question_choices_use_two_buttons_per_rich_row(tmp_path: Path) -> None:
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    question = store.create(7, "Choose", ["One", "Two", "Three", "Four", "Five"])
    fake = FakeBot()
    card = TelegramQuestionCard(cast(Bot, fake))

    await card.send(question)

    markdown = fake.api_calls[0][1]["rich_message"]["markdown"]
    assert markdown.count("<tg-button-row") == 3


async def test_question_card_surfaces_rich_message_rejection(
    tmp_path: Path,
) -> None:
    store = TelegramQuestionStore(tmp_path / "questions.sqlite3")
    question = store.create(7, "Choose", ["A", "B"])
    fake = FakeBot(reject_rich=True)
    card = TelegramQuestionCard(cast(Bot, fake))

    with pytest.raises(BadRequest, match="Rich Messages unavailable"):
        await card.send(question)


def test_question_validation_and_callback_parsing() -> None:
    prompt, choices = validate_question(" Pick one ", [" A ", " B "])
    assert prompt == "Pick one"
    assert choices == ("A", "B")

    assert parse_question_callback("question:nonce:2") == ("nonce", 2)
    assert parse_question_callback("question:nonce:not-a-number") is None
    assert parse_question_callback("turn:stop") is None

    with pytest.raises(ValueError, match="distinct"):
        validate_question("Pick", ["Same", "Same"])


def test_question_database_is_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "questions.sqlite3"
    TelegramQuestionStore(path).create(7, "Choose", ["A", "B"])

    assert path.stat().st_mode & 0o777 == 0o600
