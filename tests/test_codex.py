from pathlib import Path
from types import SimpleNamespace
from typing import cast

from openai_codex import AsyncCodex, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ItemCompletedNotification,
    MessagePhase,
    ThreadItem,
)
from openai_codex.models import AgentMessageDeltaNotification

from ariadne.codex import CodexConversation
from ariadne.the_thread import build_developer_instructions


class FakeTurn:
    def __init__(self, deltas: list[str], final_answer: str | None = None) -> None:
        self._deltas = deltas
        self._final_answer = final_answer

    async def stream(self):
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


class FakeThread:
    def __init__(self, final_answer: str | None = None) -> None:
        self._final_answer = final_answer
        self.inputs: list[str] = []
        self.turn_options: list[dict[str, object]] = []

    async def turn(self, input: str, **options: object) -> FakeTurn:
        self.inputs.append(input)
        self.turn_options.append(options)
        return FakeTurn(["Hello", " world"], self._final_answer)


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
        client=cast(AsyncCodex, client),
    )

    first = [text async for text in conversation.stream_reply("First message")]
    second = [text async for text in conversation.stream_reply("Follow-up")]

    assert first == ["Hello", "Hello world"]
    assert second == ["Hello", "Hello world"]
    assert thread.inputs == ["First message", "Follow-up"]
    assert client.thread_start_options == [
        {
            "cwd": str(tmp_path),
            "developer_instructions": build_developer_instructions(tmp_path),
            "sandbox": Sandbox.workspace_write,
        }
    ]
    assert thread.turn_options == [
        {"cwd": str(tmp_path), "sandbox": Sandbox.workspace_write},
        {"cwd": str(tmp_path), "sandbox": Sandbox.workspace_write},
    ]


async def test_codex_conversation_uses_the_final_agent_answer(tmp_path: Path) -> None:
    thread = FakeThread(final_answer="The final answer.")
    conversation = CodexConversation(
        tmp_path,
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
    conversation = CodexConversation(tmp_path, client=cast(AsyncCodex, client))

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
