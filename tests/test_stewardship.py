from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastmcp.exceptions import ToolError
from openai_codex.generated.v2_all import ReasoningEffort

from ariadne.config import StewardshipSettings
from ariadne.handoff import HandoffState
from ariadne.mcp import stewardship as stewardship_tools
from ariadne.prompts.activations import build_stewardship_turn_prompt
from ariadne.scripts import stewardship as stewardship_script
from ariadne.stewardship import (
    CYCLE_ENVIRONMENT,
    STATE_ENVIRONMENT,
    StewardshipCycle,
    StewardshipRunResult,
    StewardshipSnapshot,
    StewardshipState,
    StewardshipStateError,
)
from ariadne.stewardship.runtime import StewardshipLoop


@dataclass
class MutableClock:
    now: datetime

    def seconds(self) -> float:
        return self.now.astimezone(UTC).timestamp()


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def make_state(
    tmp_path: Path,
    clock: MutableClock,
    *identifiers: str,
) -> StewardshipState:
    values = iter(identifiers or ("cycle-1",))
    state = StewardshipState(
        tmp_path / "stewardship.sqlite3",
        clock=clock.seconds,
        id_factory=lambda: next(values),
    )
    state.initialize()
    return state


def make_settings(
    tmp_path: Path,
    *,
    enabled: bool = True,
    timezone: str = "Europe/London",
) -> StewardshipSettings:
    return StewardshipSettings(
        enabled=enabled,
        timezone=timezone,
        waking_start=time(9),
        waking_end=time(21),
        state=tmp_path / "stewardship.sqlite3",
        poll_interval_seconds=1,
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-01-15T08:59:00Z", False),
        ("2026-01-15T09:00:00Z", True),
        ("2026-01-15T20:59:00Z", True),
        ("2026-01-15T21:00:00Z", False),
        ("2026-07-15T07:59:00Z", False),
        ("2026-07-15T08:00:00Z", True),
        ("2026-07-15T20:00:00Z", False),
        ("2026-03-29T08:00:00Z", True),
        ("2026-10-25T09:00:00Z", True),
    ],
)
def test_claim_uses_local_waking_boundaries_and_dst(
    tmp_path: Path,
    now: str,
    expected: bool,
) -> None:
    current = instant(now)
    state = make_state(tmp_path, MutableClock(current))

    claimed = state.claim_due(
        now=current,
        timezone=ZoneInfo("Europe/London"),
        waking_start=time(9),
        waking_end=time(21),
    )

    assert (claimed is not None) is expected
    if claimed is not None:
        assert claimed.local_day == current.astimezone(ZoneInfo("Europe/London")).date()
        assert claimed.attempted_at.tzinfo is UTC


def test_claim_rejects_timezone_free_instants(tmp_path: Path) -> None:
    current = instant("2026-09-11T10:00:00Z")
    state = make_state(tmp_path, MutableClock(current))

    with pytest.raises(StewardshipStateError, match="explicit timezone"):
        state.claim_due(
            now=datetime(2026, 9, 11, 10),
            timezone=ZoneInfo("UTC"),
            waking_start=time(9),
            waking_end=time(21),
        )


def test_daily_cycle_does_not_replay_missed_dates(tmp_path: Path) -> None:
    clock = MutableClock(instant("2026-09-12T22:00:00+01:00"))
    state = make_state(tmp_path, clock, "cycle-13", "cycle-14")
    zone = ZoneInfo("Europe/London")

    assert (
        state.claim_due(
            now=clock.now,
            timezone=zone,
            waking_start=time(9),
            waking_end=time(21),
        )
        is None
    )
    clock.now = instant("2026-09-13T09:00:00+01:00")
    claimed = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert claimed is not None
    assert claimed.local_day.isoformat() == "2026-09-13"
    state.complete(claimed.id, fallback_summary="Reviewed today's best opportunity.")

    clock.now = instant("2026-09-13T19:00:00+01:00")
    assert (
        state.claim_due(
            now=clock.now,
            timezone=zone,
            waking_start=time(9),
            waking_end=time(21),
        )
        is None
    )
    clock.now = instant("2026-09-14T09:00:00+01:00")
    next_cycle = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert next_cycle is not None
    assert next_cycle.local_day.isoformat() == "2026-09-14"


def test_failure_waits_thirty_minutes_before_retry(tmp_path: Path) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock, "first", "retry")
    zone = ZoneInfo("Europe/London")
    first = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert first is not None
    state.fail(first.id, RuntimeError("provider unavailable"))

    clock.now = instant("2026-09-11T10:29:59+01:00")
    assert (
        state.claim_due(
            now=clock.now,
            timezone=zone,
            waking_start=time(9),
            waking_end=time(21),
        )
        is None
    )
    clock.now = instant("2026-09-11T10:30:00+01:00")
    retry = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert retry is not None
    assert retry.id == "retry"


def test_cancellation_and_startup_interruption_are_immediately_retryable(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock, "cancelled", "interrupted")
    zone = ZoneInfo("Europe/London")
    cancelled = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert cancelled is not None
    state.release(cancelled.id)
    interrupted = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert interrupted is not None

    restarted = StewardshipState(
        state.path,
        clock=clock.seconds,
        id_factory=lambda: "recovered",
    )
    assert restarted.initialize() == (interrupted.id,)
    snapshot = restarted.snapshot()
    assert snapshot.status == "idle"
    assert snapshot.last_error == "Interrupted before cycle completion"
    assert (
        restarted.claim_due(
            now=clock.now,
            timezone=zone,
            waking_start=time(9),
            waking_end=time(21),
        )
        is not None
    )


def test_pause_is_durable_supports_until_and_force_does_not_resume(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock, "forced", "resumed")
    zone = ZoneInfo("Europe/London")

    paused = state.pause()
    assert paused.paused is True
    assert paused.paused_until is None
    restarted = StewardshipState(state.path, clock=clock.seconds)
    restarted.initialize()
    assert restarted.snapshot().paused is True
    assert (
        restarted.claim_due(
            now=clock.now,
            timezone=zone,
            waking_start=time(9),
            waking_end=time(21),
        )
        is None
    )

    forced = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
        force=True,
    )
    assert forced is not None
    state.release(forced.id)
    assert state.snapshot().paused is True
    state.resume()
    resumed = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert resumed is not None


def test_pause_until_expires_atomically(tmp_path: Path) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock)
    zone = ZoneInfo("Europe/London")
    until = instant("2026-09-11T12:00:00+01:00")

    state.pause(until=until)
    clock.now = until - timedelta(seconds=1)
    assert state.snapshot().paused is True
    assert (
        state.claim_due(
            now=clock.now,
            timezone=zone,
            waking_start=time(9),
            waking_end=time(21),
        )
        is None
    )
    clock.now = until
    claimed = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert claimed is not None
    assert state.snapshot().paused is False
    assert state.snapshot().paused_until is None


def test_pause_until_requires_a_future_aware_instant(tmp_path: Path) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00Z"))
    state = make_state(tmp_path, clock)

    with pytest.raises(StewardshipStateError, match="explicit timezone"):
        state.pause(until=datetime(2026, 9, 12, 10))
    with pytest.raises(StewardshipStateError, match="future"):
        state.pause(until=clock.now)


def test_outcome_keeps_bounded_recent_orientation_and_rotates_broad_attention(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00Z"))
    state = make_state(tmp_path, clock, "first", "second")
    zone = ZoneInfo("UTC")
    first = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert first is not None
    state.record_outcome(
        first.id,
        summary="  Researched a fitting role and prepared a shortlist.  ",
        broad_attention="  Early career preferences  ",
    )
    state.complete(first.id, fallback_summary="unused")
    snapshot = state.snapshot()
    assert snapshot.recent_summary == (
        "2026-09-11: Researched a fitting role and prepared a shortlist."
    )
    assert snapshot.last_broad_attention == "Early career preferences"
    assert snapshot.last_broad_attention_at == clock.now

    clock.now = instant("2026-09-12T10:00:00Z")
    second = state.claim_due(
        now=clock.now,
        timezone=zone,
        waking_start=time(9),
        waking_end=time(21),
    )
    assert second is not None
    state.record_outcome(second.id, summary="Nothing worthwhile today.")
    state.complete(second.id, fallback_summary="unused")
    snapshot = state.snapshot()
    assert snapshot.recent_summary.endswith("2026-09-12: Nothing worthwhile today.")
    assert snapshot.last_broad_attention == "Early career preferences"
    assert snapshot.last_broad_attention_at == instant("2026-09-11T10:00:00Z")


@pytest.mark.parametrize(
    ("summary", "broad_attention", "error"),
    [
        (" ", None, "must not be empty"),
        ("x" * 4_001, None, "4000-character"),
        ("useful", "x" * 1_001, "1000-character"),
    ],
)
def test_outcome_rejects_unbounded_or_empty_state(
    tmp_path: Path,
    summary: str,
    broad_attention: str | None,
    error: str,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00Z"))
    state = make_state(tmp_path, clock)
    cycle = state.claim_due(
        now=clock.now,
        timezone=ZoneInfo("UTC"),
        waking_start=time(9),
        waking_end=time(21),
    )
    assert cycle is not None

    with pytest.raises(StewardshipStateError, match=error):
        state.record_outcome(
            cycle.id,
            summary=summary,
            broad_attention=broad_attention,
        )


def test_mcp_outcome_uses_only_the_active_cycle_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00Z"))
    state = make_state(tmp_path, clock)
    cycle = state.claim_due(
        now=clock.now,
        timezone=ZoneInfo("UTC"),
        waking_start=time(9),
        waking_end=time(21),
    )
    assert cycle is not None
    monkeypatch.setenv(STATE_ENVIRONMENT, str(state.path))
    monkeypatch.setenv(CYCLE_ENVIRONMENT, cycle.id)

    assert stewardship_tools.record_stewardship_outcome("Useful private work") == {
        "status": "recorded",
        "cycle_id": cycle.id,
    }
    state.complete(cycle.id, fallback_summary="unused")
    assert "Useful private work" in state.snapshot().recent_summary

    monkeypatch.setenv(CYCLE_ENVIRONMENT, "wrong-cycle")
    with pytest.raises(ToolError, match="not running"):
        stewardship_tools.record_stewardship_outcome("Should fail")


def test_mcp_outcome_requires_turn_scoped_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(STATE_ENVIRONMENT, raising=False)
    monkeypatch.delenv(CYCLE_ENVIRONMENT, raising=False)

    with pytest.raises(ToolError, match="authority is unavailable"):
        stewardship_tools.record_stewardship_outcome("Should fail")


class FakeConversation:
    def __init__(
        self,
        *,
        on_turn: Any | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.profile = SimpleNamespace(model="gpt-fake", effort=ReasoningEffort.low)
        self.on_turn = on_turn
        self.error = error
        self.prompts: list[str] = []
        self.closed = False

    async def stream_turn(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        if self.on_turn is not None:
            self.on_turn()
        if self.error is not None:
            raise self.error
        if False:
            yield None

    async def close(self) -> None:
        self.closed = True


def make_loop(
    tmp_path: Path,
    clock: MutableClock,
    state: StewardshipState,
    conversation: FakeConversation,
    *,
    enabled: bool = True,
    handoffs: HandoffState | None = None,
    recover_running: bool = True,
) -> StewardshipLoop:
    return StewardshipLoop(
        make_settings(tmp_path, enabled=enabled),
        tmp_path,
        tmp_path,
        SimpleNamespace(),  # type: ignore[arg-type]
        human="Divy",
        state=state,
        handoffs=handoffs,
        conversation_factory=lambda _cycle: conversation,  # type: ignore[return-value]
        clock=lambda: clock.now,
        recover_running=recover_running,
    )


async def test_runtime_success_releases_staged_handoff(tmp_path: Path) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock)
    handoffs = HandoffState(tmp_path / "telegram.sqlite3", clock=clock.seconds)
    conversation = FakeConversation(
        on_turn=lambda: handoffs.stage(
            activation_key="cycle-1",
            source="stewardship",
            body="A useful plan is ready for conversation.",
        )
    )
    loop = make_loop(tmp_path, clock, state, conversation, handoffs=handoffs)

    result = await loop.process_due()

    assert result == StewardshipRunResult(
        "completed", "cycle-1", instant("2026-09-11").date()
    )
    assert state.snapshot().status == "idle"
    retained = handoffs.for_activation("cycle-1")
    assert retained is not None
    assert retained.status == "ready"
    assert conversation.closed is True


async def test_runtime_failure_is_reported_delayed_and_discards_handoff(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock, "cycle-1", "retry")
    handoffs = HandoffState(tmp_path / "telegram.sqlite3", clock=clock.seconds)

    def stage() -> None:
        handoffs.stage(
            activation_key="cycle-1",
            source="stewardship",
            body="This must not be delivered.",
        )

    conversation = FakeConversation(on_turn=stage, error=RuntimeError("model failed"))
    loop = make_loop(tmp_path, clock, state, conversation, handoffs=handoffs)

    result = await loop.process_due()

    assert result == StewardshipRunResult(
        "failed",
        "cycle-1",
        instant("2026-09-11").date(),
        "model failed",
    )
    assert state.snapshot().status == "failed"
    retained = handoffs.for_activation("cycle-1")
    assert retained is not None
    assert retained.status == "discarded"
    assert (await loop.process_due()).status == "not-run"


async def test_runtime_cancellation_releases_cycle_and_discards_handoff(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock)
    handoffs = HandoffState(tmp_path / "telegram.sqlite3", clock=clock.seconds)

    def stage() -> None:
        handoffs.stage(
            activation_key="cycle-1",
            source="stewardship",
            body="This must not be delivered.",
        )

    conversation = FakeConversation(on_turn=stage, error=asyncio.CancelledError())
    loop = make_loop(tmp_path, clock, state, conversation, handoffs=handoffs)

    with pytest.raises(asyncio.CancelledError):
        await loop.process_due()

    assert state.snapshot().status == "idle"
    retained = handoffs.for_activation("cycle-1")
    assert retained is not None
    assert retained.status == "discarded"
    assert conversation.closed is True


def test_runtime_startup_discards_an_interrupted_handoff(tmp_path: Path) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    original = make_state(tmp_path, clock)
    cycle = original.claim_due(
        now=clock.now,
        timezone=ZoneInfo("Europe/London"),
        waking_start=time(9),
        waking_end=time(21),
    )
    assert cycle is not None
    handoffs = HandoffState(tmp_path / "telegram.sqlite3", clock=clock.seconds)
    handoffs.stage(
        activation_key=cycle.id,
        source="stewardship",
        body="Interrupted result",
    )
    restarted = StewardshipState(original.path, clock=clock.seconds)

    make_loop(
        tmp_path,
        clock,
        restarted,
        FakeConversation(),
        handoffs=handoffs,
    )

    assert restarted.snapshot().status == "idle"
    retained = handoffs.for_activation(cycle.id)
    assert retained is not None
    assert retained.status == "discarded"


async def test_disabled_runtime_only_runs_when_explicitly_forced(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-09-11T10:00:00+01:00"))
    state = make_state(tmp_path, clock)
    conversation = FakeConversation()
    loop = make_loop(tmp_path, clock, state, conversation, enabled=False)

    assert (await loop.process_due()).status == "not-run"
    result = await loop.process_due(force=True)
    assert result.status == "completed"
    assert len(conversation.prompts) == 1


def test_runtime_status_reports_local_next_cycle_and_durable_controls(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-03-28T08:00:00Z"))
    state = make_state(tmp_path, clock)
    loop = make_loop(tmp_path, clock, state, FakeConversation())

    status = loop.status_snapshot()
    assert status.enabled is True
    assert status.running is False
    assert status.next_expected_at == instant("2026-03-28T09:00:00+00:00")

    pause_until = instant("2026-03-29T10:30:00+01:00")
    paused = loop.pause(until=pause_until)
    assert paused.paused is True
    assert paused.paused_until == pause_until
    assert paused.next_expected_at == pause_until

    resumed = loop.resume()
    assert resumed.paused is False
    assert resumed.next_expected_at == instant("2026-03-28T09:00:00+00:00")

    loop.pause()
    assert loop.status_snapshot().next_expected_at is None


def test_status_moves_to_next_local_day_after_completion_across_dst(
    tmp_path: Path,
) -> None:
    clock = MutableClock(instant("2026-03-28T10:00:00Z"))
    state = make_state(tmp_path, clock)
    cycle = state.claim_due(
        now=clock.now,
        timezone=ZoneInfo("Europe/London"),
        waking_start=time(9),
        waking_end=time(21),
    )
    assert cycle is not None
    state.complete(cycle.id, fallback_summary="Finished today's cycle.")
    loop = make_loop(tmp_path, clock, state, FakeConversation())

    assert loop.status_snapshot().next_expected_at == instant(
        "2026-03-29T09:00:00+01:00"
    )


def test_activation_prompt_supplies_exact_bounded_orientation() -> None:
    awakened = instant("2026-09-11T10:00:00+01:00")
    cycle = StewardshipCycle(
        "cycle-1",
        awakened.date(),
        awakened.astimezone(UTC),
    )
    snapshot = StewardshipSnapshot(
        status="running",
        active_cycle_id=cycle.id,
        active_local_day=cycle.local_day,
        last_attempted_at=awakened.astimezone(UTC),
        last_attempted_local_day=cycle.local_day,
        last_completed_at=None,
        last_completed_local_day=None,
        recent_summary="2026-09-10: Researched roles.",
        last_broad_attention_at=instant("2026-09-01T12:00:00Z"),
        last_broad_attention="University friendships",
        last_error=None,
        paused=False,
        paused_until=None,
    )

    prompt = build_stewardship_turn_prompt(
        cycle=cycle,
        awakened_at=awakened,
        timezone="Europe/London",
        waking_start=time(9),
        waking_end=time(21),
        state=snapshot,
    )

    assert "Reflect → Dream → Choose → Act → Learn" in prompt
    assert "since=2026-09-08T09:00:00+00:00" in prompt
    assert "before=2026-09-11T09:00:00+00:00" in prompt
    assert "2026-09-10 through 2026-09-25" in prompt
    assert snapshot.recent_summary in prompt
    assert snapshot.last_broad_attention in prompt
    assert "record_stewardship_outcome" in prompt

    with pytest.raises(ValueError, match="explicit timezone"):
        build_stewardship_turn_prompt(
            cycle=cycle,
            awakened_at=datetime(2026, 9, 11, 10),
            timezone="Europe/London",
            waking_start=time(9),
            waking_end=time(21),
            state=snapshot,
        )


def test_manual_script_reports_a_failed_cycle_truthfully(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def failed(_path: Path | None) -> StewardshipRunResult:
        return StewardshipRunResult(
            "failed",
            "cycle-1",
            instant("2026-09-11").date(),
            "model failed",
        )

    monkeypatch.setattr(stewardship_script, "run_once", failed)
    monkeypatch.setattr(stewardship_script, "configure_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["ariadne-stewardship"])

    stewardship_script.main()

    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "cycle_id": "cycle-1",
        "local_day": "2026-09-11",
        "error": "model failed",
    }
