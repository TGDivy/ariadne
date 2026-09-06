import asyncio
import json
import logging
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
    McpToolCallError,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    PlanThreadItem,
    ReasoningEffort,
    ReasoningSummary,
    ReasoningSummaryTextDeltaNotification,
    ReasoningThreadItem,
    ThreadItem,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    TokenUsageBreakdown,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import AgentMessageDeltaNotification
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

import ariadne.codex.conversation as conversation_module
from ariadne.codex import (
    ActivityUpdated,
    AgentMessageCompleted,
    AgentMessageUpdated,
    CapabilityCallCompleted,
    CodexConversation,
    CodexTurnSettings,
    TurnInterrupted,
    WorkStarted,
    WorkSummaryUpdated,
    _mcp_config_overrides,
)
from ariadne.codex.resolver import resolve_profile
from ariadne.profile import MAIL_PROFILE, TELEGRAM_PROFILE
from ariadne.telemetry import Telemetry

HUMAN = "Example User"

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
    telemetry: Telemetry | None = None,
) -> CodexConversation:
    return CodexConversation(
        resolve_profile(
            TELEGRAM_PROFILE,
            vault=vault,
            settings=settings,
            human=human,
        ),
        client=client,
        telemetry=telemetry,
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

    assert not any("ARIADNE_VAULT" in value for value in overrides)
    assert 'mcp_servers.ariadne.env.ARIADNE_PROFILE="telegram"' in overrides
    assert 'mcp_servers.ariadne.args=["-m", "ariadne.mcp"]' in overrides
    assert "mcp_servers.ariadne.enabled=true" in overrides
    assert "mcp_servers.ariadne.tool_timeout_sec=960" in overrides
    assert (
        "mcp_servers.ariadne.enabled_tools="
        + json.dumps(TELEGRAM_PROFILE.enabled_tools)
        in overrides
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
        usage_updates: list[ThreadTokenUsage] | None = None,
    ) -> None:
        self._deltas = deltas
        self._final_answer = final_answer
        self._started_items = started_items or []
        self._completed_items = completed_items or []
        self._usage_updates = usage_updates or []
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

        agent = AgentMessageThreadItem(
            id="item",
            phase=MessagePhase.final_answer,
            text="",
            type="agentMessage",
        )
        yield SimpleNamespace(
            payload=ItemStartedNotification(
                item=ThreadItem(root=agent),
                startedAtMs=0,
                threadId="thread",
                turnId="turn",
            )
        )

        for delta in self._deltas:
            payload = AgentMessageDeltaNotification(
                delta=delta,
                itemId="item",
                threadId="thread",
                turnId="turn",
            )
            yield SimpleNamespace(payload=payload)

        for usage in self._usage_updates:
            payload = ThreadTokenUsageUpdatedNotification(
                threadId="thread",
                tokenUsage=usage,
                turnId="turn",
            )
            yield SimpleNamespace(payload=payload)

        item = agent.model_copy(
            update={"text": self._final_answer or "".join(self._deltas)}
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


class ReasoningSummaryTurn(FakeTurn):
    async def stream(self):
        item = ReasoningThreadItem(
            id="reasoning", content=[], summary=[], type="reasoning"
        )
        yield SimpleNamespace(
            payload=ItemStartedNotification(
                item=ThreadItem(root=item),
                startedAtMs=0,
                threadId="thread",
                turnId="turn",
            )
        )
        for delta in ("Confirming ", "the calculation"):
            yield SimpleNamespace(
                payload=ReasoningSummaryTextDeltaNotification(
                    delta=delta,
                    itemId="reasoning",
                    summaryIndex=0,
                    threadId="thread",
                    turnId="turn",
                )
            )
        yield SimpleNamespace(
            payload=ItemCompletedNotification(
                completedAtMs=0,
                item=ThreadItem(
                    root=item.model_copy(
                        update={"summary": ["Confirming the calculation"]}
                    )
                ),
                threadId="thread",
                turnId="turn",
            )
        )
        async for event in super().stream():
            yield event


class FakeThread:
    def __init__(
        self,
        *,
        final_answer: str | None = None,
        turn: FakeTurn | InterruptibleTurn | None = None,
        turns: list[FakeTurn | InterruptibleTurn] | None = None,
    ) -> None:
        self._final_answer = final_answer
        self._turn = turn
        self._turns = turns
        self.inputs: list[RunInput] = []
        self.turn_options: list[dict[str, object]] = []

    async def turn(
        self, input: RunInput, **options: object
    ) -> FakeTurn | InterruptibleTurn:
        self.inputs.append(input)
        self.turn_options.append(options)
        if self._turns is not None:
            return self._turns[len(self.inputs) - 1]
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

    first = [event async for event in conversation.stream_turn("First message")]
    second = [event async for event in conversation.stream_turn("Follow-up")]

    assert [
        event.text for event in first if isinstance(event, AgentMessageUpdated)
    ] == [
        "Hello",
        "Hello world",
    ]
    assert [
        event.text for event in second if isinstance(event, AgentMessageUpdated)
    ] == [
        "Hello",
        "Hello world",
    ]
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
            "summary": ReasoningSummary.model_validate("concise"),
        },
        {
            "approval_mode": ApprovalMode.auto_review,
            "cwd": str(tmp_path),
            "effort": ReasoningEffort.low,
            "model": "gpt-5.6-luna",
            "sandbox": Sandbox.workspace_write,
            "summary": ReasoningSummary.model_validate("concise"),
        },
    ]


async def test_codex_conversation_emits_the_cumulative_turn_usage_delta_once(
    tmp_path: Path,
) -> None:
    def usage(
        last_input_tokens: int,
        cumulative_input_tokens: int,
        cumulative_output_tokens: int,
    ) -> ThreadTokenUsage:
        last = TokenUsageBreakdown(
            inputTokens=last_input_tokens,
            cachedInputTokens=last_input_tokens // 2,
            outputTokens=5,
            reasoningOutputTokens=2,
            totalTokens=last_input_tokens + 5,
        )
        total = TokenUsageBreakdown(
            inputTokens=cumulative_input_tokens,
            cachedInputTokens=cumulative_input_tokens // 2,
            outputTokens=cumulative_output_tokens,
            reasoningOutputTokens=4,
            totalTokens=cumulative_input_tokens + cumulative_output_tokens,
        )
        return ThreadTokenUsage(last=last, total=total, modelContextWindow=100_000)

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    telemetry = Telemetry(meter_provider=provider)
    thread = FakeThread(
        turns=[
            FakeTurn(
                ["First"],
                usage_updates=[usage(10, 10, 5), usage(20, 30, 10)],
            ),
            FakeTurn(
                ["Second"],
                usage_updates=[usage(20, 50, 15)],
            ),
        ]
    )
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
        telemetry=telemetry,
    )

    _ = [event async for event in conversation.stream_turn("First question")]
    _ = [event async for event in conversation.stream_turn("Second question")]

    assert conversation.last_turn_token_usage == TokenUsageBreakdown(
        inputTokens=20,
        cachedInputTokens=10,
        outputTokens=5,
        reasoningOutputTokens=2,
        totalTokens=25,
    )

    values = {
        metric.name: tuple(
            point.value for point in metric.data.data_points if hasattr(point, "value")
        )
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert values["ariadne.codex.input_tokens"] == (50,)
    assert values["ariadne.codex.cached_input_tokens"] == (25,)
    assert values["ariadne.codex.usage_reports"] == (2,)
    assert values["ariadne.codex.flex_credits_equivalent"] == pytest.approx(
        (0.0005875,)
    )
    assert values["ariadne.codex.flex_cost_equivalent_usd"] == pytest.approx(
        (0.0000235,)
    )
    provider.shutdown()


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

    _ = [event async for event in conversation.stream_turn("Research this")]

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
    events = [event async for event in conversation.stream_turn("Research")]
    activities = [event.text for event in events if isinstance(event, ActivityUpdated)]

    assert activities == ["Searching the web…"]
    assert "private" not in activities[0]


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (
            ReasoningThreadItem(
                id="reasoning", content=[], summary=[], type="reasoning"
            ),
            "Analysing…",
        ),
        (PlanThreadItem(id="plan", text="private plan", type="plan"), "Planning…"),
    ],
)
def test_codex_reasoning_states_have_concise_private_activity_labels(
    item: object, expected: str
) -> None:
    assert conversation_module._activity_message(item) == expected
    assert "private" not in expected


async def test_codex_streams_only_the_requested_reasoning_summary(
    tmp_path: Path,
) -> None:
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(
            AsyncCodex,
            FakeCodex(FakeThread(turn=ReasoningSummaryTurn(["Done"]))),
        ),
    )

    events = [event async for event in conversation.stream_turn("Check it")]

    assert WorkStarted("reasoning", "Analysing…") in events
    assert [
        event.text for event in events if isinstance(event, WorkSummaryUpdated)
    ] == ["Confirming ", "Confirming the calculation"]


async def test_codex_conversation_reports_mcp_activity_without_tool_details(
    tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.INFO)
    mcp_item = McpToolCallThreadItem(
        arguments={"paths": ["/private/path"]},
        id="mcp",
        server="ariadne",
        status=McpToolCallStatus.in_progress,
        tool="request_telegram_file_delivery",
        type="mcpToolCall",
    )
    completed_mcp_item = mcp_item.model_copy(
        update={"duration_ms": 1250, "status": McpToolCallStatus.completed}
    )
    thread = FakeThread(
        turn=FakeTurn(
            ["Answer"],
            started_items=[ThreadItem(root=mcp_item)],
            completed_items=[ThreadItem(root=completed_mcp_item)],
        )
    )
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )
    events = [event async for event in conversation.stream_turn("Prepare the file")]
    activities = [event.text for event in events if isinstance(event, ActivityUpdated)]

    assert activities == ["Preparing files…"]
    assert (
        CapabilityCallCompleted(
            server="ariadne",
            tool="request_telegram_file_delivery",
            status="completed",
            error=None,
        )
        in events
    )
    assert "private" not in activities[0]
    assert (
        "Codex MCP call started source=telegram server=ariadne "
        "tool=request_telegram_file_delivery call_id=mcp" in caplog.text
    )
    assert (
        "Codex MCP call finished source=telegram server=ariadne "
        "tool=request_telegram_file_delivery call_id=mcp status=completed "
        "duration=1.25s" in caplog.text
    )
    assert "/private/path" not in caplog.text


def test_policy_rejected_telegram_mcp_call_logs_duration_request_and_error(
    caplog, monkeypatch
) -> None:
    caplog.set_level(logging.INFO)
    clock = iter((10.0, 21.0))
    monkeypatch.setattr(conversation_module.time, "monotonic", lambda: next(clock))
    started = McpToolCallThreadItem(
        arguments={"text": "private message"},
        id="mcp",
        server="ariadne",
        status=McpToolCallStatus.in_progress,
        tool="send_telegram_message",
        type="mcpToolCall",
    )
    failed = started.model_copy(
        update={
            "duration_ms": 0,
            "error": McpToolCallError(
                message="This action was rejected due to unacceptable risk."
            ),
            "status": McpToolCallStatus.failed,
        }
    )

    started_at = conversation_module._log_mcp_started(started, "mail")
    conversation_module._log_mcp_finished(failed, "mail", started_at)

    assert "status=failed failure=policy_rejected duration=11.00s" in caplog.text
    assert 'request={"text": "private message"}' in caplog.text
    assert "This action was rejected due to unacceptable risk." in caplog.text


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("search_knowledge", "Searching memory…"),
        ("read_knowledge", "Reading memory…"),
        ("create_knowledge", "Remembering…"),
        ("update_knowledge", "Updating memory…"),
        ("archive_knowledge", "Organising memory…"),
    ],
)
async def test_codex_conversation_reports_specific_service_activity(
    tmp_path: Path, tool: str, expected: str
) -> None:
    item = McpToolCallThreadItem(
        arguments={"query": "private query"},
        id="mail",
        server="ariadne",
        status=McpToolCallStatus.in_progress,
        tool=tool,
        type="mcpToolCall",
    )
    thread = FakeThread(
        turn=FakeTurn(["Answer"], started_items=[ThreadItem(root=item)])
    )
    conversation = make_conversation(
        tmp_path,
        DEFAULT_SETTINGS,
        human=HUMAN,
        client=cast(AsyncCodex, FakeCodex(thread)),
    )
    events = [event async for event in conversation.stream_turn("Find the email")]
    activities = [event.text for event in events if isinstance(event, ActivityUpdated)]

    assert activities == [expected]


async def test_telegram_question_is_not_announced_as_a_tool(
    tmp_path: Path,
) -> None:
    mcp_item = McpToolCallThreadItem(
        arguments={"prompt": "Which one?", "choices": ["A", "B"]},
        id="mcp",
        server="ariadne",
        status=McpToolCallStatus.in_progress,
        tool="ask_telegram_question",
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
    events = [event async for event in conversation.stream_turn("Choose")]
    activities = [event.text for event in events if isinstance(event, ActivityUpdated)]

    assert activities == []


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
        _ = [event async for event in conversation.stream_turn("Stop me")]

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
        _ = [event async for event in conversation.stream_turn("Review the vault")]

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
        _ = [event async for event in conversation.stream_turn("Review the vault")]

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
            event
            async for event in conversation.stream_turn(
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

    events = [event async for event in conversation.stream_turn("Question")]

    assert [
        event.text for event in events if isinstance(event, AgentMessageUpdated)
    ] == [
        "Hello",
        "Hello world",
    ]
    assert [
        event.text for event in events if isinstance(event, AgentMessageCompleted)
    ] == ["The final answer."]


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
        event
        async for event in conversation.stream_turn(
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

    _ = [event async for event in conversation.stream_turn("First message")]
    conversation.reset()

    assert len(client.thread_start_options) == 1

    _ = [event async for event in conversation.stream_turn("Second message")]

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

    _ = [event async for event in conversation.stream_turn("First event")]
    _ = [event async for event in conversation.stream_turn("Second event")]

    assert first_thread.inputs == ["First event"]
    assert second_thread.inputs == ["Second event"]
    assert len(client.thread_start_options) == 2
