"""A deterministic stand-in for Codex, used by tests and the dev server.

It speaks Codex's real app-server notification types, so everything above it
(the conversation wrapper, projector, core, API, and stream) runs unchanged.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    ReasoningEffort,
    ReasoningSummaryTextDeltaNotification,
    ReasoningThreadItem,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnError,
    TurnStatus,
)

from ..conversation.questions import QuestionStore
from ..conversation.shares import SharedFiles

POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class Think:
    text: str


@dataclass(frozen=True, slots=True)
class Activity:
    tool: str
    seconds: float = 0.2


@dataclass(frozen=True, slots=True)
class Say:
    text: str
    phase: Literal["commentary", "final"] = "final"
    chunk: int = 16


@dataclass(frozen=True, slots=True)
class Ask:
    prompt: str
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Share:
    paths: tuple[str, ...]
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class Sleep:
    seconds: float


@dataclass(frozen=True, slots=True)
class WaitForStop:
    pass


@dataclass(frozen=True, slots=True)
class Fail:
    message: str = "Scripted failure."


type Step = Think | Activity | Say | Ask | Share | Sleep | WaitForStop | Fail


@dataclass(frozen=True, slots=True)
class Rule:
    match: re.Pattern[str]
    steps: tuple[Step, ...]


@dataclass(frozen=True, slots=True)
class Script:
    """Ordered rules; the first whose pattern matches the turn input runs."""

    rules: tuple[Rule, ...]
    delay: float = 0.02

    def steps_for(self, text: str) -> tuple[Step, ...]:
        for rule in self.rules:
            if rule.match.search(text):
                return rule.steps
        return (Say(text="(no scripted reply)"),)


def parse_step(raw: Mapping[str, Any]) -> Step:
    if len(raw) != 1:
        raise ValueError(f"A scripted step needs exactly one key: {dict(raw)!r}")
    ((kind, value),) = raw.items()
    if kind == "think":
        return Think(str(value))
    if kind == "activity":
        if isinstance(value, Mapping):
            return Activity(str(value["tool"]), float(value.get("seconds", 0.2)))
        return Activity(str(value))
    if kind == "say":
        if isinstance(value, Mapping):
            phase = str(value.get("phase", "final"))
            if phase not in {"commentary", "final"}:
                raise ValueError(f"Unknown speech phase: {phase}")
            return Say(
                str(value["text"]),
                cast(Literal["commentary", "final"], phase),
                int(value.get("chunk", 16)),
            )
        return Say(str(value))
    if kind == "ask":
        return Ask(str(value["prompt"]), tuple(str(item) for item in value["choices"]))
    if kind == "share":
        return Share(
            tuple(str(path) for path in value["paths"]),
            str(value["caption"]) if value.get("caption") else None,
        )
    if kind == "sleep":
        return Sleep(float(value))
    if kind == "wait_for_stop":
        return WaitForStop()
    if kind == "fail":
        return Fail(str(value or "Scripted failure."))
    raise ValueError(f"Unknown scripted step: {kind}")


def parse_script(raw: Mapping[str, Any]) -> Script:
    rules = tuple(
        Rule(
            re.compile(str(rule.get("match", ".*")), re.IGNORECASE | re.DOTALL),
            tuple(parse_step(step) for step in rule["steps"]),
        )
        for rule in raw["rules"]
    )
    return Script(rules=rules, delay=float(raw.get("delay", 0.02)))


def load_script(path: Path) -> Script:
    return parse_script(yaml.safe_load(path.read_text(encoding="utf-8")))


def script_of(*rules: tuple[str, Sequence[Step]], delay: float = 0.0) -> Script:
    """Build a script inline, mainly for tests."""
    return Script(
        rules=tuple(
            Rule(re.compile(pattern, re.IGNORECASE | re.DOTALL), tuple(steps))
            for pattern, steps in rules
        ),
        delay=delay,
    )


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence):
        return "\n".join(str(getattr(item, "text", "")) for item in value).strip()
    return str(value)


@dataclass(slots=True)
class ScriptedTurn:
    """One scripted turn; steering and interruption behave like Codex's."""

    id: str
    input_text: str
    steps: tuple[Step, ...]
    script: Script
    state: Path | None
    raw_input: object = None
    steer_error: Exception | None = None
    steers: list[str] = field(default_factory=list)
    interrupted: asyncio.Event = field(default_factory=asyncio.Event)
    answers: dict[str, str] = field(default_factory=dict)

    async def steer(self, input: object) -> None:
        if self.steer_error is not None:
            raise self.steer_error
        self.steers.append(_text(input))

    async def interrupt(self) -> None:
        self.interrupted.set()

    async def stream(self) -> AsyncIterator[SimpleNamespace]:
        counter = 0
        for step in self.steps:
            if self.interrupted.is_set():
                break
            counter += 1
            item_id = f"{self.id}-item-{counter}"
            if isinstance(step, Think):
                async for event in self._think(item_id, step):
                    yield event
            elif isinstance(step, Activity):
                async for event in self._activity(item_id, step):
                    yield event
            elif isinstance(step, Say):
                async for event in self._say(item_id, step):
                    yield event
            elif isinstance(step, Ask):
                async for event in self._ask(item_id, step):
                    yield event
            elif isinstance(step, Share):
                self._share(step)
            elif isinstance(step, Sleep):
                await self._pause(step.seconds)
            elif isinstance(step, WaitForStop):
                await self.interrupted.wait()
            elif isinstance(step, Fail):
                yield self._completed(
                    TurnStatus.failed, TurnError(message=step.message)
                )
                return
        status = (
            TurnStatus.interrupted
            if self.interrupted.is_set()
            else TurnStatus.completed
        )
        yield self._completed(status)

    def _fill(self, text: str) -> str:
        values = {"input": self.input_text, "steered": " / ".join(self.steers)}
        values.update(self.answers)
        return re.sub(r"\{(\w+)\}", lambda match: values.get(match[1], match[0]), text)

    async def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self.interrupted.wait(), timeout=seconds)

    async def _think(self, item_id: str, step: Think) -> AsyncIterator[SimpleNamespace]:
        item = ReasoningThreadItem(id=item_id, content=[], summary=[], type="reasoning")
        yield self._started(item)
        text = self._fill(step.text)
        for part in _chunks(text, 12):
            await self._pause(self.script.delay)
            yield SimpleNamespace(
                payload=ReasoningSummaryTextDeltaNotification.model_validate(
                    {
                        "delta": part,
                        "itemId": item_id,
                        "summaryIndex": 0,
                        "threadId": "scripted",
                        "turnId": self.id,
                    }
                )
            )
        yield self._completed_item(item.model_copy(update={"summary": [text]}))

    async def _activity(
        self, item_id: str, step: Activity
    ) -> AsyncIterator[SimpleNamespace]:
        item = self._tool(item_id, step.tool, McpToolCallStatus.in_progress)
        yield self._started(item)
        await self._pause(step.seconds)
        yield self._completed_item(
            item.model_copy(update={"status": McpToolCallStatus.completed})
        )

    async def _say(self, item_id: str, step: Say) -> AsyncIterator[SimpleNamespace]:
        phase = (
            MessagePhase.commentary
            if step.phase == "commentary"
            else MessagePhase.final_answer
        )
        item = AgentMessageThreadItem(
            id=item_id, phase=phase, text="", type="agentMessage"
        )
        yield self._started(item)
        text = self._fill(step.text)
        for part in _chunks(text, step.chunk):
            if self.interrupted.is_set():
                return
            await self._pause(self.script.delay)
            yield SimpleNamespace(
                payload=AgentMessageDeltaNotification.model_validate(
                    {
                        "delta": part,
                        "itemId": item_id,
                        "threadId": "scripted",
                        "turnId": self.id,
                    }
                )
            )
        if self.interrupted.is_set():
            return
        yield self._completed_item(item.model_copy(update={"text": text}))

    async def _ask(self, item_id: str, step: Ask) -> AsyncIterator[SimpleNamespace]:
        if self.state is None:
            raise RuntimeError("A scripted question needs the conversation state.")
        item = self._tool(item_id, "ask_question", McpToolCallStatus.in_progress)
        yield self._started(item)
        store = QuestionStore(self.state)
        question = store.create(self._fill(step.prompt), step.choices)
        while question.status == "pending" and not self.interrupted.is_set():
            await asyncio.sleep(POLL_SECONDS)
            current = store.get(question.question_id)
            if current is None:
                break
            question = current
        if self.interrupted.is_set():
            store.cancel(question.question_id)
            return
        self.answers["answer"] = question.answer or question.status
        yield self._completed_item(
            item.model_copy(update={"status": McpToolCallStatus.completed})
        )

    def _share(self, step: Share) -> None:
        if self.state is None:
            raise RuntimeError("A scripted share needs the conversation state.")
        # Relative paths in a script are relative to the dev state directory,
        # which need not be under $HOME the way Iris's real shares must be.
        paths = tuple(
            path if path.is_absolute() else self.state.parent / path
            for path in map(Path, step.paths)
        )
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise RuntimeError(f"Scripted share files are missing: {missing}")
        SharedFiles(self.state).stage(paths, step.caption)

    def _tool(
        self, item_id: str, tool: str, status: McpToolCallStatus
    ) -> McpToolCallThreadItem:
        return McpToolCallThreadItem(
            id=item_id,
            server="ariadne",
            tool=tool,
            status=status,
            arguments={},
            type="mcpToolCall",
        )

    def _started(self, item: Any) -> SimpleNamespace:
        return SimpleNamespace(
            payload=ItemStartedNotification(
                item=ThreadItem(root=item),
                started_at_ms=0,
                thread_id="scripted",
                turn_id=self.id,
            )
        )

    def _completed_item(self, item: Any) -> SimpleNamespace:
        return SimpleNamespace(
            payload=ItemCompletedNotification(
                completed_at_ms=0,
                item=ThreadItem(root=item),
                thread_id="scripted",
                turn_id=self.id,
            )
        )

    def _completed(
        self, status: TurnStatus, error: TurnError | None = None
    ) -> SimpleNamespace:
        return SimpleNamespace(
            payload=TurnCompletedNotification(
                thread_id="scripted",
                turn=Turn(id=self.id, items=[], status=status, error=error),
            )
        )


class ScriptedThread:
    def __init__(self, client: ScriptedCodex, thread_id: str) -> None:
        self.id = thread_id
        self._client = client

    async def turn(self, input: object, **options: object) -> ScriptedTurn:
        text = _text(input)
        turn = ScriptedTurn(
            id=f"turn-{len(self._client.turns) + 1}",
            input_text=text,
            steps=self._client.script.steps_for(text),
            script=self._client.script,
            state=self._client.state,
            raw_input=input,
            steer_error=self._client.steer_error,
        )
        self._client.turns.append(turn)
        return turn


class ScriptedCodex:
    """The subset of `AsyncCodex` that Ariadne uses, driven by a script."""

    def __init__(
        self,
        script: Script,
        *,
        state: Path | None = None,
        steer_error: Exception | None = None,
    ) -> None:
        self.script = script
        self.state = state
        self.steer_error = steer_error
        self.turns: list[ScriptedTurn] = []
        self.threads: dict[str, ScriptedThread] = {}
        self.started: list[str] = []
        self.resumed: list[str] = []

    def as_client(self) -> AsyncCodex:
        # Structural stand-in: Ariadne only calls the methods defined here.
        return cast(AsyncCodex, self)

    async def thread_start(self, **options: object) -> ScriptedThread:
        thread = ScriptedThread(self, f"thread-{secrets.token_hex(4)}")
        self.threads[thread.id] = thread
        self.started.append(thread.id)
        return thread

    async def thread_resume(self, thread_id: str, **options: object) -> ScriptedThread:
        self.resumed.append(thread_id)
        thread = self.threads.get(thread_id)
        if thread is None:
            thread = ScriptedThread(self, thread_id)
            self.threads[thread_id] = thread
        return thread

    async def models(self) -> SimpleNamespace:
        efforts = (ReasoningEffort.low, ReasoningEffort.medium, ReasoningEffort.high)
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    model=name,
                    display_name=display,
                    default_reasoning_effort=ReasoningEffort.medium,
                    supported_reasoning_efforts=[
                        SimpleNamespace(reasoning_effort=effort) for effort in efforts
                    ],
                    hidden=False,
                )
                for name, display in (
                    ("gpt-5.6-luna", "Luna"),
                    ("gpt-5.6-terra", "Terra"),
                )
            ]
        )

    async def close(self) -> None:
        return None


def _chunks(text: str, size: int) -> list[str]:
    size = max(1, size)
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]
