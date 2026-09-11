from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastmcp.exceptions import ToolError
from openai_codex.generated.v2_all import MessagePhase

from ariadne.codex import AgentMessageCompleted, AgentMessageStarted
from ariadne.handoff import HandoffCoordinator, HandoffError, HandoffState
from ariadne.mcp.handoff import hand_off_to_telegram_conversation
from ariadne.prompts.activations import (
    SILENT_HANDOFF_RESPONSE,
    build_direct_turn_with_handoffs,
    build_proactive_handoff_turn_prompt,
)
from ariadne.telegram.history import TelegramMessageStore
from ariadne.telegram.proactive import ProactiveTurn
from ariadne.telegram.rich import RichBotAPI


class MutableClock:
    def __init__(self, value: float = 1_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def handoff_state(tmp_path: Path, clock: MutableClock) -> HandoffState:
    identifiers = (f"handoff-{index}" for index in range(100))
    state = HandoffState(
        tmp_path / "telegram.sqlite3",
        clock=clock,
        id_factory=lambda: next(identifiers),
    )
    state.initialize()
    return state


def test_schema_shares_private_telegram_state_and_is_additive(tmp_path: Path) -> None:
    path = tmp_path / "private" / "telegram.sqlite3"
    TelegramMessageStore(path).initialize()
    HandoffState(path).initialize()

    with sqlite3.connect(path) as database:
        tables = {
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {"telegram_messages", "telegram_handoffs"} <= tables
    assert path.stat().st_mode & 0o777 == 0o600


def test_staging_replaces_only_the_unreleased_activation(tmp_path: Path) -> None:
    clock = MutableClock()
    state = handoff_state(tmp_path, clock)

    first = state.stage(activation_key="mail:1", source="mail", body="First")
    clock.value += 1
    replacement = state.stage(
        activation_key="mail:1", source="mail", body="Much better context"
    )

    assert replacement.id != first.id
    assert replacement.body == "Much better context"
    assert state.list_status("staged") == (replacement,)

    state.release("mail:1")
    with pytest.raises(HandoffError, match="already been released"):
        state.stage(activation_key="mail:1", source="mail", body="Too late")


def test_failed_activation_is_suppressed_and_can_retry_cleanly(tmp_path: Path) -> None:
    clock = MutableClock()
    state = handoff_state(tmp_path, clock)
    state.stage(activation_key="revisit:1", source="revisit", body="Tentative")

    state.discard("revisit:1", "worker failed")

    discarded = state.for_activation("revisit:1")
    assert discarded is not None
    assert discarded.status == "discarded"
    assert discarded.error == "worker failed"
    assert state.claim_ready() == ()

    retried = state.stage(
        activation_key="revisit:1", source="revisit", body="Verified on retry"
    )
    state.release("revisit:1")

    assert retried.status == "staged"
    assert state.claim_ready()[0].body == "Verified on retry"


def test_claims_are_bounded_fifo_and_complete_durably(tmp_path: Path) -> None:
    clock = MutableClock()
    state = handoff_state(tmp_path, clock)
    for index in range(4):
        state.stage(activation_key=f"job:{index}", source="test", body=f"body {index}")
        state.release(f"job:{index}")
        clock.value += 1

    claimed = state.claim_ready(limit=3)

    assert [handoff.body for handoff in claimed] == ["body 0", "body 1", "body 2"]
    assert [handoff.body for handoff in state.list_status("ready")] == ["body 3"]

    state.complete([handoff.id for handoff in claimed])

    assert len(state.list_status("completed")) == 3
    assert all(item.completed_at is not None for item in state.list_status("completed"))


def test_restart_recovers_an_interrupted_claim_conservatively(tmp_path: Path) -> None:
    clock = MutableClock()
    state = handoff_state(tmp_path, clock)
    state.stage(activation_key="job:1", source="mail", body="Ready")
    state.release("job:1")
    claimed = state.claim_ready()
    assert claimed[0].status == "claimed"

    clock.value += 10
    restarted = HandoffState(state.path, clock=clock)
    restarted.initialize()

    recovered = restarted.list_status("ready")
    assert len(recovered) == 1
    assert recovered[0].error == "Interrupted before shared conversation completed"


def test_invalid_or_unbounded_handoffs_are_refused(tmp_path: Path) -> None:
    state = handoff_state(tmp_path, MutableClock())

    with pytest.raises(HandoffError, match="must not be empty"):
        state.stage(activation_key="job", source="mail", body=" ")
    with pytest.raises(HandoffError, match="65536-character"):
        state.stage(activation_key="job", source="mail", body="x" * 65_537)
    with pytest.raises(HandoffError, match="between 1 and 32"):
        state.claim_ready(limit=33)


class FakeTarget:
    def __init__(self) -> None:
        self.proactive_handoff_blocked = False
        self.presented: list[tuple[str, ...]] = []
        self.error: Exception | None = None

    async def present_handoffs(self, handoffs) -> None:
        self.presented.append(tuple(handoff.body for handoff in handoffs))
        if self.error is not None:
            raise self.error


async def test_coordinator_honours_quiet_window_busy_state_and_batching(
    tmp_path: Path,
) -> None:
    wall_clock = MutableClock()
    monotonic = MutableClock(0)
    state = handoff_state(tmp_path, wall_clock)
    coordinator = HandoffCoordinator(
        state,
        clock=monotonic,
        quiet_window_seconds=120,
        batch_limit=2,
    )
    target = FakeTarget()
    for index in range(3):
        state.stage(activation_key=f"job:{index}", source="test", body=f"body {index}")
        state.release(f"job:{index}")
        wall_clock.value += 1

    monotonic.value = 119
    assert await coordinator.process_ready(target) is False
    target.proactive_handoff_blocked = True
    monotonic.value = 121
    assert await coordinator.process_ready(target) is False
    target.proactive_handoff_blocked = False

    assert await coordinator.process_ready(target) is True

    assert target.presented == [("body 0", "body 1")]
    assert len(state.list_status("completed")) == 2
    assert [item.body for item in state.list_status("ready")] == ["body 2"]
    assert await coordinator.process_ready(target) is False


async def test_direct_message_claim_has_priority_and_failed_turn_retries(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    state = handoff_state(tmp_path, clock)
    coordinator = HandoffCoordinator(state, clock=clock, quiet_window_seconds=0)
    state.stage(activation_key="job", source="mail", body="Train moved")
    state.release("job")

    claimed = coordinator.claim_for_direct_turn()

    assert [item.body for item in claimed] == ["Train moved"]
    assert await coordinator.process_ready(FakeTarget()) is False
    coordinator.retry(claimed, "direct turn failed")
    assert state.list_status("ready")[0].error == "direct turn failed"


async def test_failed_proactive_turn_retains_the_claimed_batch(tmp_path: Path) -> None:
    clock = MutableClock()
    state = handoff_state(tmp_path, clock)
    coordinator = HandoffCoordinator(state, clock=clock, quiet_window_seconds=0)
    state.stage(activation_key="job", source="mail", body="Useful")
    state.release("job")
    target = FakeTarget()
    target.error = RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        await coordinator.process_ready(target)

    assert state.list_status("ready")[0].error == "model unavailable"


def test_mcp_stages_against_its_turn_scoped_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "telegram.sqlite3"
    monkeypatch.setenv("ARIADNE_TELEGRAM_STATE", str(path))
    monkeypatch.setenv("ARIADNE_ACTIVATION_KEY", "mail:42")
    monkeypatch.setenv("ARIADNE_ACTIVATION_SOURCE", "mail")

    first = hand_off_to_telegram_conversation("First context")
    second = hand_off_to_telegram_conversation("Replacement context")

    handoff = HandoffState(path).for_activation("mail:42")
    assert first["handoff_id"] != second["handoff_id"]
    assert handoff is not None
    assert handoff.body == "Replacement context"
    assert handoff.status == "staged"


def test_mcp_handoff_requires_background_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARIADNE_ACTIVATION_KEY", raising=False)

    with pytest.raises(ToolError, match="authority is unavailable"):
        hand_off_to_telegram_conversation("No activation")


def test_prompt_preserves_fifo_free_form_context_without_making_it_prose(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    state = handoff_state(tmp_path, clock)
    for key, body in (("one", "Train moved"), ("two", "Calendar updated")):
        state.stage(activation_key=key, source="test", body=body)
        state.release(key)
        clock.value += 1
    handoffs = state.claim_ready()

    direct = build_direct_turn_with_handoffs("Does 9am work?", handoffs)
    proactive = build_proactive_handoff_turn_prompt(handoffs)

    assert direct.index("Train moved") < direct.index("Calendar updated")
    assert direct.endswith(
        "<current_human_message>\nDoes 9am work?\n</current_human_message>"
    )
    assert "not Telegram prose" in direct
    assert proactive.index("Train moved") < proactive.index("Calendar updated")
    assert SILENT_HANDOFF_RESPONSE in proactive
    assert "obsolete or duplicative" in proactive


class FakeRichAPI:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))


async def test_proactive_renderer_has_no_fake_input_or_thinking_placeholder(
    tmp_path: Path,
) -> None:
    rich = FakeRichAPI()
    history = TelegramMessageStore(tmp_path / "telegram.sqlite3")
    renderer = ProactiveTurn(cast(RichBotAPI, rich), history, chat_id=7)

    assert rich.sent == []
    await renderer.apply(AgentMessageStarted("final", MessagePhase.final_answer))
    assert rich.sent == []
    await renderer.apply(
        AgentMessageCompleted(
            "final",
            MessagePhase.final_answer,
            "Also—the train now leaves at 08:40. Does that still work?",
        )
    )
    renderer.complete()

    assert [item["markdown"] for item in rich.sent] == [
        "Also—the train now leaves at 08:40. Does that still work?"
    ]
    page = history.read(7, since=datetime.min.replace(tzinfo=UTC))
    assert [(item.speaker, item.source, item.text) for item in page.messages] == [
        (
            "iris",
            "telegram",
            "Also—the train now leaves at 08:40. Does that still work?",
        )
    ]


async def test_proactive_renderer_can_suppress_an_obsolete_batch(
    tmp_path: Path,
) -> None:
    rich = FakeRichAPI()
    renderer = ProactiveTurn(
        cast(RichBotAPI, rich),
        TelegramMessageStore(tmp_path / "telegram.sqlite3"),
        chat_id=7,
    )

    await renderer.apply(AgentMessageStarted("final", MessagePhase.final_answer))
    await renderer.apply(
        AgentMessageCompleted(
            "final", MessagePhase.final_answer, SILENT_HANDOFF_RESPONSE
        )
    )
    renderer.complete()

    assert rich.sent == []
