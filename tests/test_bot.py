import asyncio
from typing import cast

from telegram import Message

from ariadne.codex import CodexConversation
from ariadne.telegram_bot import (
    BUSY_MESSAGE,
    PLACEHOLDER_TEXT,
    TELEGRAM_MESSAGE_LIMIT,
    AriadneBot,
    is_allowed_user,
)


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.edits: list[str] = []

    async def reply_text(self, text: str) -> "FakeMessage":
        self.replies.append(text)
        return self

    async def edit_text(self, text: str) -> "FakeMessage":
        self.edits.append(text)
        return self


class FakeConversation:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.prompts: list[str] = []

    async def stream_reply(self, prompt: str):
        self.prompts.append(prompt)
        for response in self._responses:
            yield response


class BlockingConversation:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_reply(self, _: str):
        self.started.set()
        yield "Working"
        await self.release.wait()
        yield "Finished"


class FakeTyping:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def send(self) -> None:
        self.calls += 1
        self.started.set()


def test_allowed_user_filter() -> None:
    assert is_allowed_user(7, 7)
    assert not is_allowed_user(8, 7)
    assert not is_allowed_user(None, 7)


def test_streamed_reply_replaces_the_placeholder_with_the_final_answer() -> None:
    async def exercise() -> None:
        conversation = FakeConversation(["Hello", "Hello, Ariadne!"])
        bot = AriadneBot(7, cast(CodexConversation, conversation))
        message = FakeMessage()

        await bot.handle_text(cast(Message, message), 7, "Say hello")

        assert conversation.prompts == ["Say hello"]
        assert message.replies == [PLACEHOLDER_TEXT]
        assert message.edits[-1] == "Hello, Ariadne!"

    asyncio.run(exercise())


def test_busy_turn_receives_a_deterministic_reply() -> None:
    async def exercise() -> None:
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

    asyncio.run(exercise())


def test_typing_indicator_runs_for_an_active_turn_and_stops_afterward() -> None:
    async def exercise() -> None:
        conversation = BlockingConversation()
        bot = AriadneBot(7, cast(CodexConversation, conversation))
        message = FakeMessage()
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

    asyncio.run(exercise())


def test_long_responses_are_split_at_telegrams_limit() -> None:
    async def exercise() -> None:
        response = "x" * (TELEGRAM_MESSAGE_LIMIT + 1)
        conversation = FakeConversation([response])
        bot = AriadneBot(7, cast(CodexConversation, conversation))
        message = FakeMessage()

        await bot.handle_text(cast(Message, message), 7, "Long answer")

        assert message.edits[-1] == "x" * TELEGRAM_MESSAGE_LIMIT
        assert message.replies == [PLACEHOLDER_TEXT, "x"]

    asyncio.run(exercise())
