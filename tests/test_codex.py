import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from openai_codex import ApprovalMode, AsyncCodex, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    ReasoningEffort,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import AgentMessageDeltaNotification

from ariadne.codex import CodexConversation, CodexTurnSettings, TurnInterrupted
from ariadne.the_thread import build_developer_instructions

DEFAULT_SETTINGS = CodexTurnSettings(
    model="gpt-5.6-sol",
    effort=ReasoningEffort.medium,
    web_search="disabled",
)


class FakeTurn:
    def __init__(
        self,
        deltas: list[str],
        *,
        final_answer: str | None = None,
        started_items: list[ThreadItem] | None = None,
    ) -> None:
        self._deltas = deltas
        self._final_answer = final_answer
        self._started_items = started_items or []
        self.interrupt_calls = 0

    async def interrupt(self) -> None:
        self.interrupt_calls += 1

    async def stream(self):
        for item in self._started_items:
            payload = ItemStartedNotification(
                item=item,
                startedAtMs=0,
                threadId="thread",
                turnId="turn",
            )
            yield SimpleNamespace(payload=payload)

        for delta in self._deltas:
            payload = AgentMessageDeltaNotification(
                delta=delta,
                itemId="item",
                threadId="thread",
                turnId="turn",
            )
            yield SimpleNamespace(payload=payload)

        if self._final_answer is not None:
            item = AgentMessageThreadItem(
                id="item",
                phase=MessagePhase.final_answer,
                text=self._final_answer,
                type="agentMessage",
            )
            payload = ItemCompletedNotification(
                completedAtMs=0,
                item=ThreadItem(root=item),
                threadId="thread",
                turnId="turn",
            )
            yield SimpleNamespace(payload=payload)


class InterruptibleTurn:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._interrupted = asyncio.Event()
        self.interrupt_calls = 0

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self._interrupted.set()

    async def stream(self):
        self.started.set()
        await self._interrupted.wait()
        payload = TurnCompletedNotification(
            threadId="thread",
            turn=Turn(id="turn", items=[], status=TurnStatus.interrupted),
        )
        yield SimpleNamespace(payload=payload)


class FakeThread:
    def __init__(
        self,
        *,
        final_answer: str | None = None,
        turn: FakeTurn | InterruptibleTurn | None = None,
    ) -> None:
        self._final_answer = final_answer
        self._turn = turn
        self.inputs: list[str] = []
        self.turn_options: list[dict[str, object]] = []

    async def turn(self, input: str, **options: object) -> FakeTurn | InterruptibleTurn:
        self.inputs.append(input)
        self.turn_options.append(options)
        if self._turn is not None:
            return self._turn
        return FakeTurn(["Hello", " world"], final_answer=self._final_answer)


class FakeCodex:
    def __init__(self, *threads: FakeThread) -> None:
        self._threads = threads
        self.thread_start_options: list[dict[str, object]] = []

    async def thread_start(self, **options: object) -> FakeThread:
        self.thread_start_options.append(options)
        return self._threads[len(self.thread_start_options) - 1]

    async def close(self) -> None:
        return None


async def test_codex_conversation_accumulates_deltas_and_reuses_its_thread(
    tmp_path: Path,
) -> None:
    thread = FakeThread()
    client = FakeCodex(thread)
    conversation = CodexConversation(
        tmp_path,
        DEFAULT_SETTINGS,
        client=cast(AsyncCodex, client),
    )

    first = [text async for text in conversation.stream_reply("First message")]
    second = [text async for text in conversation.stream_reply("Follow-up")]

    assert first == ["Hello", "Hello world"]
    assert second == ["Hello", "Hello world"]
    assert thread.inputs == ["First message", "Follow-up"]
    assert client.thread_start_options == [
        {
            "approval_mode": ApprovalMode.auto_review,
            "config": {
                "model_reasoning_effort": "medium",
                "web_search": "disabled",
            },
            "cwd": str(tmp_path),
            "developer_instructions": (
                f"{build_developer_instructions(tmp_path)}\n\n"
                "## Current information\n\n"
                "Live web search is disabled. Do not claim to have searched, "
                "researched,\nchecked, or verified current information on the web."
            ),
            "model": "gpt-5.6-sol",
            "sandbox": Sandbox.workspace_write,
        }
    ]
    assert thread.turn_options == [
        {
            "approval_mode": ApprovalMode.auto_review,
            "cwd": str(tmp_path),
            "effort": ReasoningEffort.medium,
            "model": "gpt-5.6-sol",
            "sandbox": Sandbox.workspace_write,
        },
        {
            "approval_mode": ApprovalMode.auto_review,
            "cwd": str(tmp_path),
            "effort": ReasoningEffort.medium,
            "model": "gpt-5.6-sol",
            "sandbox": Sandbox.workspace_write,
        },
    ]


async def test_codex_conversation_enables_live_web_search_explicitly(
    tmp_path: Path,
) -> None:
    thread = FakeThread()
    settings = CodexTurnSettings(
        model="gpt-5.6-sol",
        effort=ReasoningEffort.high,
        web_search="live",
    )
    client = FakeCodex(thread)
    conversation = CodexConversation(
        tmp_path,
        settings,
        client=cast(AsyncCodex, client),
    )

    _ = [text async for text in conversation.stream_reply("Research this")]

    assert client.thread_start_options[0]["config"] == {
        "model_reasoning_effort": "high",
        "web_search": "live",
        "tools": {"web_search": {"context_size": "medium"}},
    }
    assert (
        "Live web search is enabled."
        in client.thread_start_options[0]["developer_instructions"]
    )


async def test_codex_conversation_reports_only_safe_activity_messages(
    tmp_path: Path,
) -> None:
    web_search_item = WebSearchThreadItem(
        id="web",
        query="a private search query",
        type="webSearch",
    )
    thread = FakeThread(
        turn=FakeTurn(
            ["Answer"],
            started_items=[ThreadItem(root=web_search_item)],
        )
    )
    conversation = CodexConversation(
        tmp_path,
        DEFAULT_SETTINGS,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )
    activities: list[str] = []

    async def record_activity(activity: str) -> None:
        activities.append(activity)

    _ = [
        text
        async for text in conversation.stream_reply(
            "Research", activity=record_activity
        )
    ]

    assert activities == ["Searching the web…"]
    assert "private" not in activities[0]


async def test_codex_conversation_turns_an_sdk_interrupt_into_a_safe_exception(
    tmp_path: Path,
) -> None:
    turn = InterruptibleTurn()
    conversation = CodexConversation(
        tmp_path,
        DEFAULT_SETTINGS,
        client=cast(AsyncCodex, FakeCodex(FakeThread(turn=turn))),
    )

    async def consume_turn() -> None:
        _ = [text async for text in conversation.stream_reply("Stop me")]

    task = asyncio.create_task(consume_turn())
    await turn.started.wait()

    assert await conversation.interrupt() is True
    with pytest.raises(TurnInterrupted):
        await task
    assert turn.interrupt_calls == 1
    assert await conversation.interrupt() is False


async def test_codex_conversation_interrupts_a_turn_that_starts_after_stop_request(
    tmp_path: Path,
) -> None:
    turn = InterruptibleTurn()
    conversation = CodexConversation(
        tmp_path,
        DEFAULT_SETTINGS,
        client=cast(AsyncCodex, FakeCodex(FakeThread(turn=turn))),
    )

    with pytest.raises(TurnInterrupted):
        _ = [
            text
            async for text in conversation.stream_reply(
                "Stop before start",
                stop_requested=lambda: True,
            )
        ]

    assert turn.interrupt_calls == 1


async def test_codex_conversation_uses_the_final_agent_answer(tmp_path: Path) -> None:
    thread = FakeThread(final_answer="The final answer.")
    conversation = CodexConversation(
        tmp_path,
        DEFAULT_SETTINGS,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )

    responses = [text async for text in conversation.stream_reply("Question")]

    assert responses == ["Hello", "Hello world", "The final answer."]


async def test_codex_conversation_starts_a_new_thread_after_reset(
    tmp_path: Path,
) -> None:
    identity = tmp_path / "Ariadne" / "Identity.md"
    identity.parent.mkdir()
    identity.write_text("First identity.", encoding="utf-8")

    first_thread = FakeThread()
    second_thread = FakeThread()
    client = FakeCodex(first_thread, second_thread)
    conversation = CodexConversation(
        tmp_path,
        DEFAULT_SETTINGS,
        client=cast(AsyncCodex, client),
    )

    _ = [text async for text in conversation.stream_reply("First message")]
    conversation.reset()
    identity.write_text("Second identity.", encoding="utf-8")

    assert len(client.thread_start_options) == 1

    _ = [text async for text in conversation.stream_reply("Second message")]

    assert first_thread.inputs == ["First message"]
    assert second_thread.inputs == ["Second message"]
    assert len(client.thread_start_options) == 2
    assert "First identity." in client.thread_start_options[0]["developer_instructions"]
    assert (
        "Second identity." in client.thread_start_options[1]["developer_instructions"]
    )
