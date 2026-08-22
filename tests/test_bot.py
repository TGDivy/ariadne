import asyncio
from typing import cast

import pytest
from telegram import Message
from telegram.constants import ParseMode

from ariadne.codex import CodexConversation
from ariadne.telegram_bot import (
    BUSY_MESSAGE,
    FAILURE_MESSAGE,
    NEW_CONVERSATION_MESSAGE,
    PLACEHOLDER_TEXT,
    TELEGRAM_MESSAGE_LIMIT,
    AriadneBot,
)


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.edits: list[str] = []
        self.edit_parse_modes: list[ParseMode | None] = []

    async def reply_text(self, text: str) -> "FakeMessage":
        self.replies.append(text)
        return self

    async def edit_text(
        self, text: str, *, parse_mode: ParseMode | None = None
    ) -> "FakeMessage":
        self.edits.append(text)
        self.edit_parse_modes.append(parse_mode)
        return self


class FakeConversation:
    def __init__(self, responses: list[str], *, failures: int = 0) -> None:
        self._responses = responses
        self._failures = failures
        self.prompts: list[str] = []
        self.reset_calls = 0

    async def stream_reply(self, prompt: str):
        self.prompts.append(prompt)
        if self._failures:
            self._failures -= 1
            raise RuntimeError("Codex failed")
        for response in self._responses:
            yield response

    def reset(self) -> None:
        self.reset_calls += 1


class BlockingConversation:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.reset_calls = 0

    async def stream_reply(self, _: str):
        self.started.set()
        yield "Working"
        await self.release.wait()
        yield "Finished"

    def reset(self) -> None:
        self.reset_calls += 1


class FakeTyping:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def send(self) -> None:
        self.calls += 1
        self.started.set()


@pytest.fixture
def message() -> FakeMessage:
    return FakeMessage()


async def test_unauthorized_message_is_ignored(message: FakeMessage, caplog) -> None:
    conversation = FakeConversation(["This should not be sent"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 8, "Hello")

    assert conversation.prompts == []
    assert message.replies == []
    assert "Ignoring message from unauthorized Telegram user id=8" in caplog.text


async def test_streamed_reply_replaces_the_placeholder_with_the_final_answer(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(["Hello", "Hello, Ariadne!"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Say hello")

    assert conversation.prompts == ["Say hello"]
    assert message.replies == [PLACEHOLDER_TEXT]
    assert message.edits[-1] == "Hello, Ariadne!"


async def test_final_response_uses_rich_telegram_formatting(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(["**C++** with `std::vector`"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Format this")

    assert message.edits[-1] == "<b>C++</b> with <code>std::vector</code>"
    assert message.edit_parse_modes[-1] == ParseMode.HTML


async def test_new_starts_a_fresh_codex_session(message: FakeMessage) -> None:
    conversation = FakeConversation([])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_new(cast(Message, message), 7)

    assert conversation.reset_calls == 1
    assert message.replies == [NEW_CONVERSATION_MESSAGE]


async def test_busy_turn_receives_a_deterministic_reply() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage()
    second_message = FakeMessage()

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "Second")
    conversation.release.set()
    await first_turn

    assert second_message.replies == [BUSY_MESSAGE]


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


async def test_long_responses_are_split_at_telegrams_limit(
    message: FakeMessage,
) -> None:
    response = "x" * (TELEGRAM_MESSAGE_LIMIT + 1)
    conversation = FakeConversation([response])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Long answer")

    assert message.edits[-1] == "x" * TELEGRAM_MESSAGE_LIMIT
    assert message.replies == [PLACEHOLDER_TEXT, "x"]


async def test_failed_turn_replies_and_allows_the_next_turn(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(["Recovered"], failures=1)
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "First")

    next_message = FakeMessage()
    await bot.handle_text(cast(Message, next_message), 7, "Second")

    assert message.replies == [PLACEHOLDER_TEXT]
    assert message.edits == [FAILURE_MESSAGE]
    assert next_message.edits[-1] == "Recovered"
    assert conversation.prompts == ["First", "Second"]
