import asyncio
import logging
from datetime import date
from typing import cast
from unittest.mock import AsyncMock

import pytest
from openai_codex.generated.v2_all import ReasoningEffort
from telegram import Message
from telegram.constants import ParseMode
from telegram.error import TimedOut

from ariadne.codex import (
    CodexConversation,
    CodexModel,
    CodexTurnSettings,
    TurnInterrupted,
)
from ariadne.telegram import bot as telegram_bot
from ariadne.telegram.bot import (
    BUSY_MESSAGE,
    DOCUMENT_WITHOUT_CAPTION,
    FAILURE_MESSAGE,
    NEW_CONVERSATION_MESSAGE,
    NOTHING_TO_STOP_MESSAGE,
    SETTINGS_BUSY_MESSAGE,
    STEERED_MESSAGE,
    STEERING_FAILED_MESSAGE,
    STOPPED_MESSAGE,
    STOPPING_MESSAGE,
    document_message,
    turn_text,
)
from ariadne.telegram.bot import AriadneBot as TelegramBot
from ariadne.telegram.format import TELEGRAM_MESSAGE_LIMIT, split_for_telegram

DEFAULT_SETTINGS = CodexTurnSettings(
    model="gpt-5.6-luna",
    effort=ReasoningEffort.low,
    web_search="disabled",
)
DEFAULT_MODELS = (
    CodexModel(
        identifier="gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        default_effort=ReasoningEffort.low,
        supported_efforts=(ReasoningEffort.low, ReasoningEffort.medium),
    ),
)


def AriadneBot(allowed_user_id: int, conversation: CodexConversation) -> TelegramBot:
    """Build the bot with the required test credential."""
    return TelegramBot(allowed_user_id, conversation, bot_token="token-for-test")


class FakeMessage:
    def __init__(self, message_id: int = 11) -> None:
        self.chat_id = 7
        self.message_id = message_id
        self.media_group_id: str | None = None
        self.text: str | None = None
        self.caption: str | None = None
        self.reply_to_message: FakeMessage | None = None
        self.replies: list[str] = []
        self.reply_parse_modes: list[ParseMode | None] = []
        self.reply_markups: list[object | None] = []
        self.edits: list[str] = []
        self.edit_parse_modes: list[ParseMode | None] = []
        self.edit_markups: list[object | None] = []
        self.drafts: list[str | None] = []
        self.draft_ids: list[int] = []

    async def reply_text(
        self,
        text: str,
        *,
        parse_mode: ParseMode | None = None,
        reply_markup: object | None = None,
    ) -> "FakeMessage":
        self.replies.append(text)
        self.reply_parse_modes.append(parse_mode)
        self.reply_markups.append(reply_markup)
        return self

    async def reply_text_draft(self, draft_id: int, text: str | None = None) -> bool:
        self.draft_ids.append(draft_id)
        self.drafts.append(text)
        return True

    async def edit_text(
        self,
        text: str,
        *,
        parse_mode: ParseMode | None = None,
        reply_markup: object | None = None,
    ) -> "FakeMessage":
        self.edits.append(text)
        self.edit_parse_modes.append(parse_mode)
        self.edit_markups.append(reply_markup)
        return self


class FakeConversation:
    def __init__(
        self,
        responses: list[str],
        *,
        failures: int = 0,
        activities: list[str] | None = None,
        spoken: list[str] | None = None,
        models: tuple[CodexModel, ...] = DEFAULT_MODELS,
    ) -> None:
        self._responses = responses
        self._failures = failures
        self._activities = activities or []
        self._spoken = spoken or []
        self._models = models
        self.settings = DEFAULT_SETTINGS
        self.prompts: list[str] = []
        self.reset_calls = 0
        self.set_settings_calls = 0
        self.interrupt_calls = 0

    async def stream_reply(
        self,
        prompt: str,
        *,
        image_paths=(),
        activity=None,
        spoken=None,
        stop_requested=None,
    ):
        self.prompts.append(prompt)
        if self._failures:
            self._failures -= 1
            raise RuntimeError("Codex failed")
        if activity is not None:
            for update in self._activities:
                await activity(update)
        if spoken is not None:
            for said in self._spoken:
                spoken(said)
        for response in self._responses:
            yield response

    def reset(self) -> None:
        self.reset_calls += 1

    def set_settings(self, settings: CodexTurnSettings) -> None:
        self.settings = settings
        self.set_settings_calls += 1
        self.reset()

    async def available_models(self) -> tuple[CodexModel, ...]:
        return self._models

    async def interrupt(self) -> bool:
        self.interrupt_calls += 1
        return False


class BlockingConversation:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted = False
        self.interrupt_calls = 0
        self.settings = DEFAULT_SETTINGS
        self.reset_calls = 0
        self.steered: list[str] = []
        self.steer_error: Exception | None = None
        self.steer_accepted = True

    async def stream_reply(
        self,
        _: str,
        *,
        image_paths=(),
        activity=None,
        spoken=None,
        stop_requested=None,
    ):
        self.started.set()
        yield "Working"
        await self.release.wait()
        if self.interrupted:
            raise TurnInterrupted()
        yield "Finished"

    def reset(self) -> None:
        self.reset_calls += 1

    def set_settings(self, settings: CodexTurnSettings) -> None:
        self.settings = settings
        self.reset()

    async def available_models(self) -> tuple[CodexModel, ...]:
        return DEFAULT_MODELS

    async def steer(self, text: str, *, image_paths=()) -> bool:
        if self.steer_error is not None:
            raise self.steer_error
        if not self.steer_accepted:
            return False
        self.steered.append(text)
        return True

    async def interrupt(self) -> bool:
        self.interrupt_calls += 1
        self.interrupted = True
        self.release.set()
        return True


class FakeTyping:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def send(self) -> None:
        self.calls += 1
        self.started.set()


class TimedOutTyping(FakeTyping):
    async def send(self) -> None:
        self.calls += 1
        self.started.set()
        raise TimedOut


class TimedOutDraftMessage(FakeMessage):
    async def reply_text_draft(self, draft_id: int, text: str | None = None) -> bool:
        raise TimedOut


@pytest.fixture
def message() -> FakeMessage:
    return FakeMessage()


@pytest.fixture
def unthrottled_drafts(monkeypatch) -> None:
    monkeypatch.setattr(telegram_bot, "DRAFT_INTERVAL_SECONDS", 0.0)


async def test_unauthorized_message_is_ignored(message: FakeMessage, caplog) -> None:
    conversation = FakeConversation(["This should not be sent"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 8, "Hello")

    assert conversation.prompts == []
    assert message.replies == []
    assert "Ignoring message from unauthorized Telegram user id=8" in caplog.text


async def test_file_delivery_uses_the_configured_bot_token(
    message: FakeMessage,
) -> None:
    delivery = AsyncMock()
    delivery.approve.return_value = [object()]
    bot = TelegramBot(
        7,
        cast(CodexConversation, FakeConversation([])),
        bot_token="configured-token",
    )
    bot._file_delivery = delivery

    await bot._approve_staged_files(cast(Message, message), 7, "approval-id")

    delivery.approve.assert_awaited_once_with(
        "approval-id", token="configured-token", chat_id=7
    )


async def test_a_streamed_reply_leaves_only_the_final_answer_behind(
    message: FakeMessage, caplog
) -> None:
    caplog.set_level(logging.INFO)
    conversation = FakeConversation(["Hello", "Hello, Ariadne!"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Say hello")

    assert conversation.prompts == [turn_text("Say hello", 11)]
    assert message.replies == ["Hello, Ariadne!"]
    assert message.edits == []
    assert message.draft_ids == [11]
    assert "Telegram message received message_id=11" in caplog.text
    assert "Telegram turn started message_id=11" in caplog.text
    assert "Telegram turn finished message_id=11 status=success" in caplog.text
    assert "Say hello" not in caplog.text


async def test_telegram_reply_includes_the_replied_message_text() -> None:
    conversation = FakeConversation(["It means the cache was effective."])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    replied_message = FakeMessage(message_id=10)
    replied_message.text = "The cache hit rate was 96%."
    message = FakeMessage(message_id=11)
    message.reply_to_message = replied_message

    await bot.handle_text(cast(Message, message), 7, "What does this mean?")

    assert conversation.prompts == [
        "Telegram reply context (message id 10):\n"
        "<quoted_message>\n"
        "The cache hit rate was 96%.\n"
        "</quoted_message>\n\n"
        "What does this mean?\n\n"
        "Telegram message id: 11"
    ]


def test_telegram_reply_uses_the_replied_message_caption() -> None:
    replied_message = FakeMessage(message_id=10)
    replied_message.caption = "The first dashboard after setup"

    prompt = turn_text("Why is this empty?", 11, cast(Message, replied_message))

    assert "The first dashboard after setup" in prompt
    assert "Telegram reply context (message id 10)" in prompt


async def test_streamed_text_is_shown_as_an_ephemeral_draft(
    message: FakeMessage, unthrottled_drafts: None
) -> None:
    conversation = FakeConversation(["Hello", "Hello, Ariadne!"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Say hello")

    assert message.drafts[0] is None
    assert "Hello" in message.drafts
    assert message.replies == ["Hello, Ariadne!"]


async def test_final_response_uses_rich_telegram_formatting(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(["**C++** with `std::vector`"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Format this")

    assert message.replies == ["<b>C++</b> with <code>std::vector</code>"]
    assert message.reply_parse_modes == [ParseMode.HTML]


async def test_safe_activity_status_is_shown_before_the_answer(
    message: FakeMessage, unthrottled_drafts: None
) -> None:
    conversation = FakeConversation(
        ["The answer"],
        activities=["Searching the web…"],
    )
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Research this")

    assert "Searching the web…" in message.drafts
    assert message.replies == ["The answer"]


async def test_an_answer_iris_already_sent_herself_is_not_repeated(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(
        ["This is the latest one."],
        spoken=["This is the latest one."],
    )
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Find my CV")

    assert message.replies == []


async def test_a_final_answer_beyond_what_iris_sent_is_still_delivered(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(
        ["Found two. The newer one is from June."],
        spoken=["Looking through your projects."],
    )
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Find my CV")

    assert message.replies == ["Found two. The newer one is from June."]


async def test_new_starts_a_fresh_codex_session(message: FakeMessage) -> None:
    conversation = FakeConversation([])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_new(cast(Message, message), 7)

    assert conversation.reset_calls == 1
    assert message.replies == [NEW_CONVERSATION_MESSAGE]


async def test_message_during_an_active_turn_steers_it_instead_of_being_rejected() -> (
    None
):
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage()
    second_message = FakeMessage()

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_text(
        cast(Message, second_message), 7, "Actually check the other file too"
    )
    conversation.release.set()
    await first_turn

    assert conversation.steered == [turn_text("Actually check the other file too", 11)]
    assert conversation.interrupt_calls == 0
    assert second_message.replies == [STEERED_MESSAGE]
    assert first_message.replies == ["Finished"]


async def test_reply_context_is_preserved_when_steering_an_active_turn() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage(message_id=10)
    second_message = FakeMessage(message_id=11)
    second_message.reply_to_message = first_message
    first_message.text = "Review the vault"

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, first_message.text)
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "Focus on this request")
    conversation.release.set()
    await first_turn

    assert len(conversation.steered) == 1
    assert "Review the vault" in conversation.steered[0]
    assert "Telegram reply context (message id 10)" in conversation.steered[0]


async def test_steering_failure_leaves_the_active_turn_running() -> None:
    conversation = BlockingConversation()
    conversation.steer_error = RuntimeError("Codex refused the steering input")
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage()
    second_message = FakeMessage()

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "One more thing")
    conversation.release.set()
    await first_turn

    assert second_message.replies == [STEERING_FAILED_MESSAGE]
    assert first_message.replies == ["Finished"]


async def test_message_sent_before_codex_accepts_the_turn_says_it_is_busy() -> None:
    conversation = BlockingConversation()
    conversation.steer_accepted = False
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage()
    second_message = FakeMessage()

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "One more thing")
    conversation.release.set()
    await first_turn

    assert conversation.steered == []
    assert second_message.replies == [BUSY_MESSAGE]
    assert first_message.replies == ["Finished"]


async def test_new_does_not_interrupt_an_active_turn() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    active_message = FakeMessage()
    new_message = FakeMessage()

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_new(cast(Message, new_message), 7)
    conversation.release.set()
    await turn

    assert conversation.reset_calls == 0
    assert new_message.replies == [BUSY_MESSAGE]


async def test_stop_interrupts_the_active_turn_and_frees_the_conversation() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    active_message = FakeMessage()
    stop_message = FakeMessage()
    new_message = FakeMessage()

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_stop(cast(Message, stop_message), 7)
    await turn
    await bot.handle_new(cast(Message, new_message), 7)

    assert conversation.interrupt_calls == 1
    assert stop_message.replies == [STOPPING_MESSAGE]
    assert stop_message.edits == [STOPPED_MESSAGE]
    assert active_message.replies == []
    assert new_message.replies == [NEW_CONVERSATION_MESSAGE]


async def test_stop_when_idle_has_a_clear_response(message: FakeMessage) -> None:
    bot = AriadneBot(7, cast(CodexConversation, FakeConversation([])))

    await bot.handle_stop(cast(Message, message), 7)

    assert message.replies == [NOTHING_TO_STOP_MESSAGE]


async def test_settings_change_model_effort_and_web_mode(message: FakeMessage) -> None:
    fast_model = CodexModel(
        identifier="gpt-fast",
        display_name="GPT Fast",
        default_effort=ReasoningEffort.low,
        supported_efforts=(ReasoningEffort.low,),
    )
    thorough_model = CodexModel(
        identifier="gpt-thorough",
        display_name="GPT Thorough",
        default_effort=ReasoningEffort.high,
        supported_efforts=(ReasoningEffort.medium, ReasoningEffort.high),
    )
    conversation = FakeConversation([], models=(fast_model, thorough_model))
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_settings(cast(Message, message), 7)
    await bot.handle_settings_callback(
        cast(Message, message),
        7,
        "settings:model:gpt-thorough",
    )
    await bot.handle_settings_callback(
        cast(Message, message),
        7,
        "settings:effort:high",
    )
    await bot.handle_settings_callback(
        cast(Message, message),
        7,
        "settings:web:live",
    )

    assert message.replies[0].startswith("Ariadne settings")
    assert message.reply_markups[0] is not None
    assert conversation.settings == CodexTurnSettings(
        model="gpt-thorough",
        effort=ReasoningEffort.high,
        web_search="live",
    )
    assert conversation.set_settings_calls == 3
    assert conversation.reset_calls == 3
    assert "Web research: Live" in message.edits[-1]


async def test_settings_rejects_changes_during_an_active_turn() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    active_message = FakeMessage()
    settings_message = FakeMessage()

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_settings_callback(
        cast(Message, settings_message),
        7,
        "settings:web:live",
    )
    conversation.release.set()
    await turn

    assert conversation.settings.web_search == "disabled"
    assert settings_message.edits == [SETTINGS_BUSY_MESSAGE]


async def test_typing_indicator_runs_for_an_active_turn_and_stops_afterward(
    message: FakeMessage,
) -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    typing = FakeTyping()

    turn = asyncio.create_task(
        bot.handle_text(
            cast(Message, message),
            7,
            "First",
            send_typing=typing.send,
        )
    )
    await conversation.started.wait()
    await typing.started.wait()
    conversation.release.set()
    await turn

    calls_after_turn = typing.calls
    await asyncio.sleep(0)
    assert calls_after_turn == 1
    assert typing.calls == calls_after_turn


async def test_typing_timeout_is_logged_without_a_traceback(
    message: FakeMessage, caplog
) -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    typing = TimedOutTyping()

    turn = asyncio.create_task(
        bot.handle_text(
            cast(Message, message),
            7,
            "First",
            send_typing=typing.send,
        )
    )
    await conversation.started.wait()
    await typing.started.wait()
    conversation.release.set()
    await turn

    timeout_log = next(
        record
        for record in caplog.records
        if record.getMessage() == "Telegram typing indicator timed out; will retry."
    )
    assert timeout_log.exc_info is None


async def test_draft_timeout_is_logged_without_a_traceback(caplog) -> None:
    message = TimedOutDraftMessage()
    draft = telegram_bot._Draft(cast(Message, message))

    await draft.keep_alive()
    await draft.keep_alive()

    timeout_logs = [
        record
        for record in caplog.records
        if record.getMessage() == "Telegram draft update timed out; will retry."
    ]
    assert len(timeout_logs) == 1
    assert timeout_logs[0].exc_info is None


async def test_long_responses_are_split_at_telegrams_limit(
    message: FakeMessage,
) -> None:
    response = "x" * (TELEGRAM_MESSAGE_LIMIT + 1)
    conversation = FakeConversation([response])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Long answer")

    assert message.replies == ["x" * TELEGRAM_MESSAGE_LIMIT, "x"]


@pytest.mark.parametrize("separator", ["\n\n", "\n", " "])
def test_long_text_prefers_readable_telegram_split_boundaries(separator: str) -> None:
    suffix = "y" * 25
    response = "x" * (TELEGRAM_MESSAGE_LIMIT - 20 - len(separator)) + separator + suffix

    chunks = split_for_telegram(response)

    assert chunks == [response[: -len(suffix)], suffix]
    assert "".join(chunks) == response
    assert all(len(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)


def test_long_text_avoids_a_tiny_telegram_message_before_a_hard_split() -> None:
    response = "intro\n\n" + "x" * TELEGRAM_MESSAGE_LIMIT

    chunks = split_for_telegram(response)

    assert chunks[0] == response[:TELEGRAM_MESSAGE_LIMIT]
    assert "".join(chunks) == response


async def test_failed_turn_replies_and_allows_the_next_turn(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(["Recovered"], failures=1)
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "First")

    next_message = FakeMessage()
    await bot.handle_text(cast(Message, next_message), 7, "Second")

    assert message.replies == [FAILURE_MESSAGE]
    assert next_message.replies == ["Recovered"]
    assert conversation.prompts == [turn_text("First", 11), turn_text("Second", 11)]


def test_document_message_uses_the_caption_as_the_request(tmp_path) -> None:
    path = tmp_path / "cv.pdf"

    text = document_message(
        "compare this with the one in my repo", [(path, "application/pdf")]
    )

    assert text.startswith("compare this with the one in my repo")
    assert f"Attached file: {path} (application/pdf)" in text


def test_document_message_without_a_caption_invents_no_task(tmp_path) -> None:
    path = tmp_path / "rows.csv"

    text = document_message(None, [(path, None)])

    assert text == f"{DOCUMENT_WITHOUT_CAPTION}\n\nAttached file: {path}"


async def test_document_sent_during_a_turn_steers_it_and_is_kept(
    tmp_path,
) -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    attachment = tmp_path / "sent" / "rows.csv"
    attachment.parent.mkdir()
    attachment.write_text("a,b\n1,2\n", encoding="utf-8")

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, FakeMessage()), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_document(
        cast(Message, FakeMessage()), 7, attachment, caption="what looks odd here?"
    )

    assert conversation.steered == [
        turn_text(document_message("what looks odd here?", [(attachment, None)]), 11)
    ]

    conversation.release.set()
    await first_turn

    assert attachment.exists()


async def test_document_is_kept_after_the_turn_it_started(tmp_path) -> None:
    conversation = FakeConversation(["Looked at it"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    attachment = tmp_path / "sent" / "notes.txt"
    attachment.parent.mkdir()
    attachment.write_text("hello", encoding="utf-8")

    await bot.handle_document(cast(Message, FakeMessage()), 7, attachment)

    assert conversation.prompts == [
        turn_text(document_message(None, [(attachment, None)]), 11)
    ]
    assert attachment.exists()


async def test_a_media_group_becomes_one_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram_bot, "ALBUM_DEBOUNCE_SECONDS", 0.01)
    conversation = FakeConversation(["Looked at them"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    message = FakeMessage()
    message.media_group_id = "album-1"
    paths = []
    for name in ("first.csv", "second.csv"):
        path = tmp_path / name.removesuffix(".csv") / name
        path.parent.mkdir()
        path.write_text("a,b\n", encoding="utf-8")
        paths.append(path)

    await bot.handle_document(
        cast(Message, message), 7, paths[0], caption="what looks odd here?"
    )
    await bot.handle_document(cast(Message, message), 7, paths[1])

    assert conversation.prompts == []

    await asyncio.sleep(0.05)

    assert conversation.prompts == [
        turn_text(
            document_message(
                "what looks odd here?", [(paths[0], None), (paths[1], None)]
            ),
            11,
        )
    ]


def test_attachments_are_kept_under_a_folder_for_today(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram_bot, "ATTACHMENT_ROOT", tmp_path)

    first = telegram_bot.attachment_path("cv.pdf")
    first.write_bytes(b"one")
    second = telegram_bot.attachment_path("cv.pdf")

    assert first.parent == tmp_path / date.today().isoformat()
    assert first.name == "cv.pdf"
    assert second.name == "cv-2.pdf"
