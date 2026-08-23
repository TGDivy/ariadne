import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    LocalImageInput,
    RunInput,
    Sandbox,
    TextInput,
)
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    ReasoningEffort,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import AgentMessageDeltaNotification

from ariadne.codex import (
    CodexConversation,
    CodexTurnSettings,
    TurnInterrupted,
    _mcp_config_overrides,
)
from ariadne.codex.resolver import resolve_profile
from ariadne.profile import MAIL_PROFILE, TELEGRAM_PROFILE

HUMAN = "Divy"

DEFAULT_SETTINGS = CodexTurnSettings(
    model="gpt-5.6-luna",
    effort=ReasoningEffort.low,
    web_search="disabled",
)


def make_conversation(
    vault: Path,
    settings: CodexTurnSettings,
    *,
    human: str,
    client: AsyncCodex,
) -> CodexConversation:
    return CodexConversation(
        resolve_profile(
            TELEGRAM_PROFILE,
            vault=vault,
            settings=settings,
            human=human,
        ),
        client=client,
    )


def test_mcp_config_forwards_its_required_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-for-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "123")

    overrides = _mcp_config_overrides(
        resolve_profile(
            TELEGRAM_PROFILE,
            vault=tmp_path,
            settings=DEFAULT_SETTINGS,
            human=HUMAN,
            mcp_environment={
                "TELEGRAM_BOT_TOKEN": "token-for-test",
                "TELEGRAM_ALLOWED_USER_ID": "123",
            },
        )
    )

    assert f'mcp_servers.ariadne.env.ARIADNE_VAULT="{tmp_path}"' in overrides
    assert 'mcp_servers.ariadne.env.ARIADNE_PROFILE="telegram"' in overrides
    assert "mcp_servers.ariadne.enabled=true" in overrides
    assert (
        "mcp_servers.ariadne.enabled_tools="
        '["runtime_status", "send_message", "react", "prepare_files"]' in overrides
    )
    assert 'mcp_servers.ariadne.env.TELEGRAM_BOT_TOKEN="token-for-test"' in overrides
    assert 'mcp_servers.ariadne.env.TELEGRAM_ALLOWED_USER_ID="123"' in overrides


class FakeTurn:
    def __init__(
        self,
        deltas: list[str],
        *,
        final_answer: str | None = None,
        started_items: list[ThreadItem] | None = None,
        completed_items: list[ThreadItem] | None = None,
    ) -> None:
        self._deltas = deltas
        self._final_answer = final_answer
        self._started_items = started_items or []
        self._completed_items = completed_items or []
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

        for item in self._completed_items:
            payload = ItemCompletedNotification(
                completedAtMs=0,
                item=item,
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
        self.steer_inputs: list[RunInput] = []

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self._interrupted.set()

    async def steer(self, input: RunInput) -> None:
        self.steer_inputs.append(input)

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
        self.inputs: list[RunInput] = []
        self.turn_options: list[dict[str, object]] = []

    async def turn(
        self, input: RunInput, **options: object
    ) -> FakeTurn | InterruptibleTurn:
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
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
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
            "base_instructions": conversation.profile.base_instructions,
            "config": {
                "model_reasoning_effort": "low",
                "web_search": "disabled",
            },
            "cwd": str(tmp_path),
            "developer_instructions": conversation.profile.developer_instructions,
            "model": "gpt-5.6-luna",
            "sandbox": Sandbox.workspace_write,
        }
    ]
    assert thread.turn_options == [
        {
            "approval_mode": ApprovalMode.auto_review,
            "cwd": str(tmp_path),
            "effort": ReasoningEffort.low,
            "model": "gpt-5.6-luna",
            "sandbox": Sandbox.workspace_write,
        },
        {
            "approval_mode": ApprovalMode.auto_review,
            "cwd": str(tmp_path),
            "effort": ReasoningEffort.low,
            "model": "gpt-5.6-luna",
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
    conversation = make_conversation(
        tmp_path,
        settings,
        human=HUMAN,
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
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
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


async def test_codex_conversation_reports_mcp_activity_without_tool_details(
    tmp_path: Path,
) -> None:
    mcp_item = McpToolCallThreadItem(
        arguments={"paths": ["/private/path"]},
        id="mcp",
        server="ariadne",
        status=McpToolCallStatus.in_progress,
        tool="prepare_files",
        type="mcpToolCall",
    )
    thread = FakeThread(
        turn=FakeTurn(
            ["Answer"],
            started_items=[ThreadItem(root=mcp_item)],
        )
    )
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )
    activities: list[str] = []

    async def record_activity(activity: str) -> None:
        activities.append(activity)

    _ = [
        text
        async for text in conversation.stream_reply(
            "Prepare the file", activity=record_activity
        )
    ]

    assert activities == ["Using Ariadne's local capability…"]
    assert "private" not in activities[0]


async def test_iris_speaking_for_herself_is_not_announced_as_a_tool(
    tmp_path: Path,
) -> None:
    mcp_item = McpToolCallThreadItem(
        arguments={"text": "Found the repo."},
        id="mcp",
        server="ariadne",
        status=McpToolCallStatus.in_progress,
        tool="send_message",
        type="mcpToolCall",
    )
    thread = FakeThread(
        turn=FakeTurn(["Answer"], started_items=[ThreadItem(root=mcp_item)])
    )
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )
    activities: list[str] = []

    async def record_activity(activity: str) -> None:
        activities.append(activity)

    _ = [
        text
        async for text in conversation.stream_reply(
            "Find my CV", activity=record_activity
        )
    ]

    assert activities == []


async def test_codex_conversation_reports_what_iris_said_in_telegram_herself(
    tmp_path: Path,
) -> None:
    def spoke(tool: str, arguments: object) -> ThreadItem:
        return ThreadItem(
            root=McpToolCallThreadItem(
                arguments=arguments,
                id=tool,
                server="ariadne",
                status=McpToolCallStatus.completed,
                tool=tool,
                type="mcpToolCall",
            )
        )

    thread = FakeThread(
        turn=FakeTurn(
            ["Done"],
            completed_items=[
                spoke("send_message", {"text": "Found the repo."}),
                spoke("send_message", '{"text": "This is the latest one."}'),
                spoke("prepare_files", {"paths": ["/home/iris/cv.pdf"]}),
            ],
        )
    )
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )
    spoken: list[str] = []

    _ = [
        text
        async for text in conversation.stream_reply("Find my CV", spoken=spoken.append)
    ]

    assert spoken == ["Found the repo.", "This is the latest one."]


async def test_codex_conversation_turns_an_sdk_interrupt_into_a_safe_exception(
    tmp_path: Path,
) -> None:
    turn = InterruptibleTurn()
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
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


async def test_codex_conversation_steers_the_turn_it_is_already_running(
    tmp_path: Path,
) -> None:
    turn = InterruptibleTurn()
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(FakeThread(turn=turn))),
    )

    assert await conversation.steer("Nothing is running yet") is False

    async def consume_turn() -> None:
        _ = [text async for text in conversation.stream_reply("Review the vault")]

    task = asyncio.create_task(consume_turn())
    await turn.started.wait()

    assert await conversation.steer("Actually check the other file too") is True
    assert turn.steer_inputs == ["Actually check the other file too"]
    assert turn.interrupt_calls == 0

    await conversation.interrupt()
    with pytest.raises(TurnInterrupted):
        await task

    assert await conversation.steer("The turn is over") is False


async def test_codex_conversation_steers_with_a_local_image_attachment(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screenshot.png"
    image_path.write_bytes(b"not a real image")
    turn = InterruptibleTurn()
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(FakeThread(turn=turn))),
    )

    async def consume_turn() -> None:
        _ = [text async for text in conversation.stream_reply("Review the vault")]

    task = asyncio.create_task(consume_turn())
    await turn.started.wait()

    steered = await conversation.steer("Look at this too", image_paths=(image_path,))

    assert steered is True
    assert turn.steer_inputs == [
        [TextInput("Look at this too"), LocalImageInput(str(image_path))]
    ]

    await conversation.interrupt()
    with pytest.raises(TurnInterrupted):
        await task


async def test_codex_conversation_interrupts_a_turn_that_starts_after_stop_request(
    tmp_path: Path,
) -> None:
    turn = InterruptibleTurn()
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
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
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )

    responses = [text async for text in conversation.stream_reply("Question")]

    assert responses == ["Hello", "Hello world", "The final answer."]


async def test_codex_conversation_sends_local_images_with_caption(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "screenshot.png"
    image_path.write_bytes(b"not a real image")
    thread = FakeThread()
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )

    _ = [
        text
        async for text in conversation.stream_reply(
            "What is shown here?", image_paths=(image_path,)
        )
    ]

    assert thread.inputs == [
        [TextInput("What is shown here?"), LocalImageInput(str(image_path))]
    ]


async def test_codex_conversation_starts_a_new_thread_after_reset(
    tmp_path: Path,
) -> None:
    first_thread = FakeThread()
    second_thread = FakeThread()
    client = FakeCodex(first_thread, second_thread)
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, client),
    )

    _ = [text async for text in conversation.stream_reply("First message")]
    conversation.reset()

    assert len(client.thread_start_options) == 1

    _ = [text async for text in conversation.stream_reply("Second message")]

    assert first_thread.inputs == ["First message"]
    assert second_thread.inputs == ["Second message"]
    assert len(client.thread_start_options) == 2


async def test_fresh_per_event_profile_starts_a_new_thread_after_each_turn(
    tmp_path: Path,
) -> None:
    first_thread = FakeThread()
    second_thread = FakeThread()
    client = FakeCodex(first_thread, second_thread)
    conversation = CodexConversation(
        resolve_profile(
            MAIL_PROFILE,
            vault=tmp_path,
            settings=DEFAULT_SETTINGS,
            human=HUMAN,
            mcp_environment={
                "ARIADNE_MAIL_JOB_ID": "INBOX:1:2",
                "ARIADNE_MAIL_STATE": str(tmp_path / "mail.sqlite3"),
            },
        ),
        client=cast(AsyncCodex, client),
    )

    _ = [text async for text in conversation.stream_reply("First event")]
    _ = [text async for text in conversation.stream_reply("Second event")]

    assert first_thread.inputs == ["First event"]
    assert second_thread.inputs == ["Second event"]
    assert len(client.thread_start_options) == 2
