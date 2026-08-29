from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from openai_codex.generated.v2_all import ReasoningEffort

from ariadne.config import RevisitSettings
from ariadne.mcp import revisit as revisit_tools
from ariadne.profile import REVISIT_PROFILES
from ariadne.prompts.activations import build_revisit_turn_prompt
from ariadne.revisit import (
    ATTENTION_SETTINGS,
    STATE_ENVIRONMENT,
    Attention,
    RevisitError,
    RevisitState,
    parse_due_at,
)
from ariadne.revisit.runtime import RevisitLoop


def instant(value: float) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def state_at(tmp_path: Path, now: float = 1_000) -> RevisitState:
    state = RevisitState(
        tmp_path / "revisits.sqlite3",
        clock=lambda: now,
        id_factory=lambda: "revisit_test",
    )
    state.initialize()
    return state


def test_attention_mapping_is_explicit_and_exhaustive() -> None:
    assert set(ATTENTION_SETTINGS) == set(Attention)
    assert ATTENTION_SETTINGS[Attention.light].model == "gpt-5.6-luna"
    assert ATTENTION_SETTINGS[Attention.light].effort == ReasoningEffort.low
    assert ATTENTION_SETTINGS[Attention.focused].model == "gpt-5.6-luna"
    assert ATTENTION_SETTINGS[Attention.focused].effort == ReasoningEffort.high
    assert ATTENTION_SETTINGS[Attention.deep].model == "gpt-5.6-terra"
    assert ATTENTION_SETTINGS[Attention.deep].effort == ReasoningEffort.medium
    assert all(setting.web_search == "live" for setting in ATTENTION_SETTINGS.values())
    assert {profile.name for profile in REVISIT_PROFILES.values()} == {
        "revisit-light",
        "revisit-focused",
        "revisit-deep",
    }


def test_revisit_times_require_a_timezone_and_normalize_to_utc() -> None:
    assert parse_due_at("2030-01-02T10:30:00+05:30") == datetime(
        2030, 1, 2, 5, tzinfo=UTC
    )
    assert parse_due_at("2030-01-02T05:00:00Z") == datetime(2030, 1, 2, 5, tzinfo=UTC)
    with pytest.raises(ValueError, match="explicit timezone"):
        parse_due_at("2030-01-02T10:30:00")


def test_state_schedules_changes_lists_and_cancels(tmp_path: Path) -> None:
    state = state_at(tmp_path)

    created = state.schedule(
        due_at=instant(2_000),
        note="  Check whether transport is settled.  ",
        attention=Attention.focused,
    )
    changed = state.change(
        created.id,
        due_at=instant(2_500),
        note="Check transport and bib details.",
        attention=Attention.deep,
    )

    assert created.id == "revisit_test"
    assert created.note == "Check whether transport is settled."
    assert changed.due_at == instant(2_500)
    assert changed.attention is Attention.deep
    assert state.list_open() == (changed,)

    state.cancel(created.id)

    assert state.get(created.id) is None
    assert state.list_open() == ()


def test_state_rejects_past_empty_and_timezone_free_revisits(tmp_path: Path) -> None:
    state = state_at(tmp_path)

    with pytest.raises(RevisitError, match="future"):
        state.schedule(due_at=instant(999), note="Later", attention=Attention.light)
    with pytest.raises(RevisitError, match="useful note"):
        state.schedule(due_at=instant(2_000), note="   ", attention=Attention.light)
    with pytest.raises(RevisitError, match="explicit timezone"):
        state.schedule(
            due_at=datetime(2030, 1, 1),
            note="Later",
            attention=Attention.light,
        )


def test_due_claiming_is_ordered_and_completed_history_is_retained(
    tmp_path: Path,
) -> None:
    identifiers = iter(("later", "earlier"))
    state = RevisitState(
        tmp_path / "revisits.sqlite3",
        clock=lambda: 1_000,
        id_factory=lambda: next(identifiers),
    )
    state.initialize()
    state.schedule(due_at=instant(3_000), note="Later", attention=Attention.light)
    state.schedule(due_at=instant(2_000), note="Earlier", attention=Attention.focused)

    assert state.claim_due(now=instant(1_999)) is None
    claimed = state.claim_due(now=instant(4_000))

    assert claimed is not None
    assert claimed.id == "earlier"
    assert claimed.status == "running"
    assert claimed.attempts == 1

    state.complete(claimed.id)

    completed = state.get(claimed.id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.completed_at == instant(1_000)
    assert [revisit.id for revisit in state.list_open()] == ["later"]


def test_startup_recovers_an_interrupted_running_revisit(tmp_path: Path) -> None:
    state = state_at(tmp_path)
    state.schedule(
        due_at=instant(2_000), note="Check again", attention=Attention.focused
    )
    assert state.claim_due(now=instant(3_000)) is not None

    recovered = RevisitState(state.path, clock=lambda: 1_100)
    recovered.initialize()

    revisit = recovered.get("revisit_test")
    assert revisit is not None
    assert revisit.status == "pending"
    assert revisit.error == "Interrupted before completion"


def test_mcp_style_initialization_does_not_requeue_active_work(tmp_path: Path) -> None:
    state = state_at(tmp_path)
    state.schedule(
        due_at=instant(2_000), note="Check again", attention=Attention.focused
    )
    assert state.claim_due(now=instant(3_000)) is not None

    capability_state = RevisitState(state.path, clock=lambda: 1_100)
    capability_state.initialize(recover_running=False)

    active = capability_state.get("revisit_test")
    assert active is not None
    assert active.status == "running"


def test_failed_revisit_is_visible_and_an_edit_requeues_it(tmp_path: Path) -> None:
    state = state_at(tmp_path)
    state.schedule(
        due_at=instant(2_000), note="Check again", attention=Attention.focused
    )
    claimed = state.claim_due(now=instant(3_000))
    assert claimed is not None
    state.fail(claimed.id, RuntimeError("Codex unavailable"))

    failed = state.list_open()[0]
    assert failed.status == "failed"
    assert failed.error == "Codex unavailable"

    changed = state.change(failed.id, due_at=instant(4_000))
    assert changed.status == "pending"
    assert changed.error is None


def test_mcp_operations_expose_semantic_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "mcp-revisits.sqlite3"
    monkeypatch.setenv(STATE_ENVIRONMENT, str(state_path))
    revisit_tools._STATES.clear()

    created = revisit_tools.schedule_wakeup(
        "2030-01-02T10:30:00+00:00",
        "Check whether the application reply arrived.",
        Attention.focused,
    )["revisit"]
    assert isinstance(created, dict)
    identifier = str(created["id"])
    assert created["attention"] == "focused"
    assert set(created) == {"id", "at", "note", "attention", "status"}

    listed = revisit_tools.list_wakeups()
    assert listed["count"] == 1

    changed = revisit_tools.update_wakeup(identifier, attention=Attention.deep)[
        "revisit"
    ]
    assert isinstance(changed, dict)
    assert changed["attention"] == "deep"

    assert revisit_tools.cancel_wakeup(identifier) == {
        "id": identifier,
        "status": "cancelled",
    }


def test_mcp_operations_require_configured_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(STATE_ENVIRONMENT, raising=False)
    revisit_tools._STATES.clear()

    with pytest.raises(ToolError, match="not configured"):
        revisit_tools.list_wakeups()


class FakeConversation:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.profile = SimpleNamespace(model="gpt-fake", effort=ReasoningEffort.low)
        self.error = error
        self.prompts: list[str] = []
        self.closed = False

    async def stream_turn(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        if False:
            yield None

    async def close(self) -> None:
        self.closed = True


def revisit_loop(
    tmp_path: Path,
    state: RevisitState,
    conversation: FakeConversation,
) -> RevisitLoop:
    return RevisitLoop(
        RevisitSettings(state.path, 15),
        tmp_path,
        lambda attention: ATTENTION_SETTINGS[attention],
        human="Divy",
        state=state,
        conversation_factory=lambda _revisit: conversation,  # type: ignore[return-value]
        clock=lambda: instant(3_000),
    )


async def test_runtime_wakes_once_and_discards_native_output(tmp_path: Path) -> None:
    state = state_at(tmp_path)
    scheduled = state.schedule(
        due_at=instant(2_000),
        note="Check whether race preparation still has gaps.",
        attention=Attention.focused,
    )
    conversation = FakeConversation()
    loop = revisit_loop(tmp_path, state, conversation)

    assert await loop.process_due() is True
    assert await loop.process_due() is False

    completed = state.get(scheduled.id)
    assert completed is not None
    assert completed.status == "completed"
    assert conversation.closed is True
    assert len(conversation.prompts) == 1
    prompt = conversation.prompts[0]
    assert "Ariadne speaking" in prompt
    assert "you asked me to schedule" in prompt
    assert "Attention you selected: focused" in prompt
    assert scheduled.note in prompt
    assert "deserves Divy's attention" in prompt
    assert "<earlier_iris_note>" in prompt


async def test_runtime_retains_a_failed_execution_without_rerouting(
    tmp_path: Path,
) -> None:
    state = state_at(tmp_path)
    scheduled = state.schedule(
        due_at=instant(2_000), note="Check once", attention=Attention.light
    )
    conversation = FakeConversation(error=RuntimeError("model failed"))
    loop = revisit_loop(tmp_path, state, conversation)

    assert await loop.process_due() is True
    assert await loop.process_due() is False

    failed = state.get(scheduled.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.attempts == 1
    assert failed.error == "model failed"


async def test_runtime_cancellation_returns_the_revisit_to_pending(
    tmp_path: Path,
) -> None:
    class CancelledConversation(FakeConversation):
        async def stream_turn(self, prompt: str) -> Any:
            self.prompts.append(prompt)
            raise asyncio.CancelledError
            yield

    state = state_at(tmp_path)
    scheduled = state.schedule(
        due_at=instant(2_000), note="Check once", attention=Attention.light
    )
    conversation = CancelledConversation()
    loop = revisit_loop(tmp_path, state, conversation)

    with pytest.raises(asyncio.CancelledError):
        await loop.process_due()

    pending = state.get(scheduled.id)
    assert pending is not None
    assert pending.status == "pending"
    assert conversation.closed is True


def test_activation_prompt_uses_the_configured_human_name(tmp_path: Path) -> None:
    revisit = state_at(tmp_path).schedule(
        due_at=instant(2_000), note="Check once", attention=Attention.light
    )

    prompt = build_revisit_turn_prompt(
        note=revisit.note,
        due_at=revisit.due_at,
        awakened_at=instant(3_000),
        attention=revisit.attention.value,
        human="Example User",
    )

    assert "deserves Example User's attention" in prompt
    assert "Divy" not in prompt
