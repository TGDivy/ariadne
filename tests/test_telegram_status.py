from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from datetime import time as daytime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from openai_codex.generated.v2_all import MessagePhase, ReasoningEffort
from telegram import Bot, InlineKeyboardMarkup

from ariadne.codex import (
    AgentMessageCompleted,
    AgentMessageStarted,
    CodexConversation,
    CodexTurnSettings,
)
from ariadne.handoff import HandoffCoordinator, HandoffState
from ariadne.revisit.models import Attention
from ariadne.revisit.state import RevisitState
from ariadne.stewardship.models import (
    StewardshipRunResult,
    StewardshipRuntimeSnapshot,
)
from ariadne.telegram.bot import WAKEUPS_UNAVAILABLE_MESSAGE
from ariadne.telegram.bot import AriadneBot as TelegramBot
from ariadne.telegram.panels import TelegramControlPanel, TelegramControlPanelStore
from ariadne.telegram.rich import RichBotAPI
from ariadne.telegram.status import (
    STATUS_DELIVER_CALLBACK,
    STATUS_INITIATIVE_CALLBACK,
    STATUS_INITIATIVE_PAUSE_CALLBACK,
    STATUS_INITIATIVE_PAUSE_DAY_CALLBACK,
    STATUS_INITIATIVE_RESUME_CALLBACK,
    STATUS_INITIATIVE_RUN_CALLBACK,
    STATUS_ROOT_CALLBACK,
    STATUS_SETTINGS_CALLBACK,
    STATUS_WAKEUP_ASK_PREFIX,
    STATUS_WAKEUP_CONFIRM_PREFIX,
    STATUS_WAKEUPS_PREFIX,
    PrivateSource,
    StatusSources,
    WakeupCounts,
    page_bounds,
    render_status,
    render_wakeups,
    wakeup_counts,
)

OWNER = 7
LONDON = ZoneInfo("Europe/London")
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

SETTINGS = CodexTurnSettings(
    model="gpt-5.6-luna",
    effort=ReasoningEffort.low,
    web_search="disabled",
)


class FakeMessage:
    def __init__(self, message_id: int = 11) -> None:
        self.chat_id = OWNER
        self.message_id = message_id
        self.replies: list[str] = []
        self.reply_markups: list[object | None] = []
        self.reply_disable_notifications: list[bool] = []
        self.edits: list[str] = []
        self.edit_markups: list[object | None] = []
        self.deleted = False

    async def reply_text(
        self,
        text: str,
        *,
        parse_mode: object | None = None,
        reply_markup: object | None = None,
        disable_notification: bool = False,
    ) -> FakeMessage:
        self.replies.append(text)
        self.reply_markups.append(reply_markup)
        self.reply_disable_notifications.append(disable_notification)
        return self

    async def edit_text(
        self,
        text: str,
        *,
        parse_mode: object | None = None,
        reply_markup: object | None = None,
    ) -> FakeMessage:
        self.edits.append(text)
        self.edit_markups.append(reply_markup)
        return self

    async def delete(self) -> bool:
        self.deleted = True
        return True


class FakeBoundBot:
    def __init__(self) -> None:
        self.deleted: list[tuple[int, int]] = []

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True


class FakeRichAPI:
    """Records the proactive sends a delivery turn actually produced."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=900 + len(self.sent), date=NOW)


class FakeConversation:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.settings = SETTINGS
        self._responses = responses or []

    async def stream_turn(
        self, prompt: str, **_: object
    ) -> AsyncIterator[AgentMessageStarted | AgentMessageCompleted]:
        del prompt
        for index, response in enumerate(self._responses):
            item = f"final-{index}"
            yield AgentMessageStarted(item, MessagePhase.final_answer)
            yield AgentMessageCompleted(item, MessagePhase.final_answer, response)


class FakeInitiative:
    """A stewardship double that records the controls the panel invoked."""

    def __init__(self, snapshot: StewardshipRuntimeSnapshot) -> None:
        self._snapshot = snapshot
        self.paused_until: list[datetime | None] = []
        self.resumed = 0
        self.forced_runs = 0

    def status_snapshot(self) -> StewardshipRuntimeSnapshot:
        return self._snapshot

    def pause(self, *, until: datetime | None = None) -> StewardshipRuntimeSnapshot:
        self.paused_until.append(until)
        self._snapshot = _replace_snapshot(
            self._snapshot, paused=True, paused_until=until
        )
        return self._snapshot

    def resume(self) -> StewardshipRuntimeSnapshot:
        self.resumed += 1
        self._snapshot = _replace_snapshot(
            self._snapshot, paused=False, paused_until=None
        )
        return self._snapshot

    async def process_due(self, *, force: bool = False) -> StewardshipRunResult:
        self.forced_runs += 1
        return StewardshipRunResult("completed")


def _replace_snapshot(
    snapshot: StewardshipRuntimeSnapshot, **changes: object
) -> StewardshipRuntimeSnapshot:
    fields = {
        name: getattr(snapshot, name) for name in StewardshipRuntimeSnapshot.__slots__
    }
    fields.update(changes)
    return StewardshipRuntimeSnapshot(**fields)  # type: ignore[arg-type]


def snapshot(**changes: object) -> StewardshipRuntimeSnapshot:
    base = StewardshipRuntimeSnapshot(
        enabled=True,
        timezone="Europe/London",
        waking_start=daytime(9, 0),
        waking_end=daytime(21, 0),
        paused=False,
        paused_until=None,
        running=False,
        active_cycle_id=None,
        last_completed_at=datetime(2026, 9, 10, 8, 32, tzinfo=UTC),
        last_completed_local_day=None,
        next_expected_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        last_error=None,
    )
    return _replace_snapshot(base, **changes)


class Panel:
    """One bot wired to real durable state and a fake stewardship loop."""

    def __init__(
        self, tmp_path: Path, responses: list[str] | None = None, **sources: object
    ) -> None:
        state = tmp_path / "telegram.sqlite3"
        self.revisits = RevisitState(tmp_path / "revisits.sqlite3")
        self.revisits.initialize()
        self.handoffs = HandoffCoordinator(HandoffState(state), quiet_window_seconds=0)
        self.initiative = FakeInitiative(snapshot())
        self.panels = TelegramControlPanelStore(state)
        self.bot = TelegramBot(
            OWNER,
            cast(CodexConversation, FakeConversation(responses)),
            bot_token="token-for-test",
            question_state=state,
            handoff_coordinator=self.handoffs,
            status_sources=StatusSources(
                timezone=LONDON,
                sources=(
                    PrivateSource("Mail", True),
                    PrivateSource("Calendar", True),
                    PrivateSource("Health", False),
                    PrivateSource("Knowledge", True),
                ),
                initiative=cast(object, self.initiative),  # type: ignore[arg-type]
                wakeups=self.revisits,
                **sources,  # type: ignore[arg-type]
            ),
        )
        self.rich = FakeRichAPI()
        self.bound = FakeBoundBot()
        self.bot._bot = cast(Bot, self.bound)
        self.bot._rich_api = cast(RichBotAPI, self.rich)

    @property
    def spoken(self) -> list[str]:
        return [str(item["markdown"]) for item in self.rich.sent]

    def schedule(self, minutes: int, note: str) -> str:
        return self.revisits.schedule(
            due_at=NOW + timedelta(minutes=minutes),
            note=note,
            attention=Attention.focused,
        ).id


def buttons(markup: object) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]


def labels(markup: object) -> list[str]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button.text for row in markup.inline_keyboard for button in row]


def test_page_bounds_clamp_a_requested_page_into_range() -> None:
    assert page_bounds(0, 3) == (1, 1, 0)
    assert page_bounds(7, 1) == (1, 2, 0)
    assert page_bounds(7, 2) == (2, 2, 5)
    assert page_bounds(7, 99) == (2, 2, 5)
    assert page_bounds(7, -4) == (1, 2, 0)


def test_status_counts_separate_upcoming_work_from_failures(tmp_path: Path) -> None:
    state = RevisitState(tmp_path / "revisits.sqlite3")
    state.initialize()
    failing = state.schedule(
        due_at=NOW + timedelta(hours=1), note="failed", attention=Attention.light
    )
    state.schedule(
        due_at=NOW + timedelta(hours=2), note="pending", attention=Attention.light
    )
    state.claim_due(now=NOW + timedelta(minutes=90))
    state.fail(failing.id, RuntimeError("provider outage"))

    counts = wakeup_counts(state.list_open())

    assert counts == WakeupCounts(upcoming=1, failed=1)


def test_status_renders_local_time_and_does_not_claim_connectivity() -> None:
    text = render_status(
        working=False,
        snapshot=snapshot(),
        counts=WakeupCounts(upcoming=2, failed=1),
        waiting_handoffs=3,
        sources=(PrivateSource("Mail", True), PrivateSource("Health", False)),
        model="gpt-5.6-luna",
        effort="low",
        web_search="off",
        timezone=LONDON,
    )

    assert "Iris: ready" in text
    assert "Initiative: ready" in text
    # 08:32 UTC is 09:32 in the configured British summer-time zone.
    assert "Last cycle: Thu 10 Sep, 09:32" in text
    assert "Next expected: Fri 11 Sep, 09:00" in text
    assert "Wake-ups: 2 upcoming, 1 failed" in text
    assert "Waiting updates: 3" in text
    assert "Enabled: Mail" in text
    assert "Health" not in text
    assert "connectivity is not measured" in text
    assert "Telegram: gpt-5.6-luna, low effort, web off" in text
    assert "Times are Europe/London." in text


def test_status_reports_a_paused_or_running_initiative_truthfully() -> None:
    paused = render_status(
        working=True,
        snapshot=snapshot(
            paused=True, paused_until=datetime(2026, 9, 12, 6, 0, tzinfo=UTC)
        ),
        counts=WakeupCounts(0, 0),
        waiting_handoffs=0,
        sources=(),
        model="m",
        effort="low",
        web_search="off",
        timezone=LONDON,
    )
    assert "Iris: working on a turn" in paused
    assert "Initiative: paused until Sat 12 Sep, 07:00" in paused

    running = render_status(
        working=False,
        snapshot=snapshot(running=True, last_error="Codex refused the turn"),
        counts=WakeupCounts(0, 0),
        waiting_handoffs=0,
        sources=(),
        model="m",
        effort="low",
        web_search="off",
        timezone=LONDON,
    )
    assert "Initiative: running now" in running
    assert "Last error: Codex refused the turn" in running

    off = render_status(
        working=False,
        snapshot=snapshot(enabled=False),
        counts=WakeupCounts(0, 0),
        waiting_handoffs=0,
        sources=(),
        model="m",
        effort="low",
        web_search="off",
        timezone=LONDON,
    )
    assert "Initiative: off" in off


def test_wakeups_page_is_bounded_and_shows_failure_state(tmp_path: Path) -> None:
    state = RevisitState(tmp_path / "revisits.sqlite3")
    state.initialize()
    for index in range(7):
        state.schedule(
            due_at=NOW + timedelta(hours=index + 1),
            note=f"note {index}",
            attention=Attention.light,
        )
    broken = state.schedule(
        due_at=NOW + timedelta(minutes=5),
        note="this one broke",
        attention=Attention.deep,
    )
    state.claim_due(now=NOW + timedelta(minutes=10))
    state.fail(broken.id, RuntimeError("calendar unreachable"))

    first, shown, current, pages = render_wakeups(
        state.list_open(), page=1, timezone=LONDON
    )

    assert len(shown) == 5
    assert (current, pages) == (1, 2)
    assert "1. Fri 11 Sep, 13:05 - deep - failed" in first
    assert "calendar unreachable" in first
    assert "Page 1 of 2, 8 open." in first
    assert "reschedule or reword" in first

    second, shown, current, pages = render_wakeups(
        state.list_open(), page=2, timezone=LONDON
    )
    assert len(shown) == 3
    assert (current, pages) == (2, 2)


def test_wakeups_page_reports_an_empty_schedule() -> None:
    text, shown, current, pages = render_wakeups((), page=1, timezone=LONDON)

    assert "Nothing is scheduled." in text
    assert (shown, current, pages) == ((), 1, 1)


async def test_status_command_opens_one_silent_panel_and_removes_the_command(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    panel.schedule(60, "check the visa reply")
    command = FakeMessage(31)

    await panel.bot.handle_status(cast(object, command), OWNER)  # type: ignore[arg-type]

    assert command.reply_disable_notifications == [True]
    assert "Ariadne status" in command.replies[0]
    assert command.deleted is True
    assert buttons(command.reply_markups[0]) == [
        f"{STATUS_WAKEUPS_PREFIX}1",
        STATUS_INITIATIVE_CALLBACK,
        STATUS_SETTINGS_CALLBACK,
    ]
    assert panel.panels.get(OWNER) == TelegramControlPanel(OWNER, 31, "status")


async def test_wakeups_command_opens_the_page_directly(tmp_path: Path) -> None:
    panel = Panel(tmp_path)
    panel.schedule(60, "check the visa reply")
    command = FakeMessage(32)

    await panel.bot.handle_wakeups(cast(object, command), OWNER)  # type: ignore[arg-type]

    assert "Wake-ups" in command.replies[0]
    assert "check the visa reply" in command.replies[0]
    assert panel.panels.get(OWNER) == TelegramControlPanel(OWNER, 32, "status")


async def test_status_panel_is_refused_for_anyone_but_the_owner(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    command = FakeMessage(33)

    await panel.bot.handle_status(cast(object, command), OWNER + 1)  # type: ignore[arg-type]
    await panel.bot.handle_wakeups(cast(object, command), None)  # type: ignore[arg-type]
    await panel.bot.handle_status_callback(
        cast(object, command),  # type: ignore[arg-type]
        OWNER + 1,
        STATUS_INITIATIVE_PAUSE_CALLBACK,
    )

    assert command.replies == []
    assert command.edits == []
    assert panel.initiative.paused_until == []


async def test_offering_delivery_only_appears_while_updates_wait(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    panel.handoffs.state.stage(
        activation_key="cycle-1", source="stewardship", body="Found two openings"
    )
    panel.handoffs.state.release("cycle-1")
    command = FakeMessage(34)

    await panel.bot.handle_status(cast(object, command), OWNER)  # type: ignore[arg-type]

    assert "Waiting updates: 1" in command.replies[0]
    assert STATUS_DELIVER_CALLBACK in buttons(command.reply_markups[0])
    assert "Deliver 1 waiting now" in labels(command.reply_markups[0])


async def test_requesting_delivery_now_hands_the_chat_back_to_conversation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)
    panel = Panel(tmp_path, responses=["Found two openings"])
    panel.handoffs.state.stage(
        activation_key="cycle-1", source="stewardship", body="Two roles worth a look"
    )
    panel.handoffs.state.release("cycle-1")
    message = FakeMessage(42)
    panel.panels.set(TelegramControlPanel(OWNER, 42, "status"))

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_DELIVER_CALLBACK,
    )

    assert panel.spoken == ["Found two openings"]
    # The delivery turn removed the panel; nothing should try to edit it back.
    assert message.edits == []
    assert panel.panels.get(OWNER) is None
    assert caplog.text == ""


async def test_delivery_with_nothing_waiting_just_refreshes_the_panel(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    message = FakeMessage(43)
    panel.panels.set(TelegramControlPanel(OWNER, 43, "status"))

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_DELIVER_CALLBACK,
    )

    assert panel.spoken == []
    assert "Ariadne status" in message.edits[-1]
    assert panel.panels.get(OWNER) == TelegramControlPanel(OWNER, 43, "status")


async def test_wakeup_cancellation_needs_a_second_trusted_confirmation(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    identifier = panel.schedule(60, "check the visa reply")
    message = FakeMessage(35)
    panel.panels.set(TelegramControlPanel(OWNER, 35, "status"))

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        f"{STATUS_WAKEUP_ASK_PREFIX}{identifier}",
    )

    assert "Cancel this wake-up?" in message.edits[-1]
    assert "check the visa reply" in message.edits[-1]
    assert buttons(message.edit_markups[-1]) == [
        f"{STATUS_WAKEUP_CONFIRM_PREFIX}{identifier}",
        f"{STATUS_WAKEUPS_PREFIX}1",
    ]
    assert len(panel.revisits.list_open()) == 1

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        f"{STATUS_WAKEUP_CONFIRM_PREFIX}{identifier}",
    )

    assert panel.revisits.list_open() == ()
    assert "Nothing is scheduled." in message.edits[-1]


async def test_a_stale_wakeup_callback_is_harmless(tmp_path: Path) -> None:
    panel = Panel(tmp_path)
    identifier = panel.schedule(60, "check the visa reply")
    message = FakeMessage(36)
    panel.panels.set(TelegramControlPanel(OWNER, 36, "status"))
    panel.revisits.cancel(identifier)

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        f"{STATUS_WAKEUP_ASK_PREFIX}{identifier}",
    )
    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        f"{STATUS_WAKEUP_CONFIRM_PREFIX}{identifier}",
    )
    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        f"{STATUS_WAKEUPS_PREFIX}not-a-page",
    )

    assert all("Nothing is scheduled." in edit for edit in message.edits)


async def test_initiative_controls_pause_resume_and_run_without_a_model_turn(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    message = FakeMessage(37)
    panel.panels.set(TelegramControlPanel(OWNER, 37, "status"))

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_INITIATIVE_CALLBACK,
    )
    assert "Initiative" in message.edits[-1]
    assert "Waking window 09:00-21:00 Europe/London." in message.edits[-1]
    assert buttons(message.edit_markups[-1]) == [
        STATUS_INITIATIVE_RUN_CALLBACK,
        STATUS_INITIATIVE_PAUSE_DAY_CALLBACK,
        STATUS_INITIATIVE_PAUSE_CALLBACK,
        STATUS_ROOT_CALLBACK,
    ]

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_INITIATIVE_PAUSE_DAY_CALLBACK,
    )
    assert panel.initiative.paused_until[-1] is not None
    assert STATUS_INITIATIVE_RESUME_CALLBACK in buttons(message.edit_markups[-1])

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_INITIATIVE_RESUME_CALLBACK,
    )
    assert panel.initiative.resumed == 1

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_INITIATIVE_PAUSE_CALLBACK,
    )
    assert panel.initiative.paused_until[-1] is None


async def test_run_now_starts_one_background_cycle_and_returns_immediately(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    message = FakeMessage(38)
    panel.panels.set(TelegramControlPanel(OWNER, 38, "status"))

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_INITIATIVE_RUN_CALLBACK,
    )
    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_INITIATIVE_RUN_CALLBACK,
    )
    task = panel.bot._initiative_task
    assert task is not None
    await task

    # The second press landed while the first cycle was still owned.
    assert panel.initiative.forced_runs == 1


async def test_the_panel_moves_between_the_status_and_settings_trees(
    tmp_path: Path,
) -> None:
    panel = Panel(tmp_path)
    message = FakeMessage(39)
    panel.panels.set(TelegramControlPanel(OWNER, 39, "status"))

    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        STATUS_SETTINGS_CALLBACK,
    )

    assert "Ariadne settings" in message.edits[-1]
    assert panel.panels.get(OWNER) == TelegramControlPanel(OWNER, 39, "settings")

    await panel.bot.handle_settings_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        "settings:status",
    )

    assert "Ariadne status" in message.edits[-1]
    assert panel.panels.get(OWNER) == TelegramControlPanel(OWNER, 39, "status")


async def test_unreadable_wakeup_state_degrades_without_breaking_the_panel(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenRevisits(RevisitState):
        def list_open(self) -> tuple[()]:
            raise sqlite3.OperationalError("revisits unavailable")

    panel = Panel(tmp_path)
    panel.bot._status = StatusSources(  # type: ignore[union-attr]
        timezone=LONDON,
        sources=(),
        initiative=cast(object, panel.initiative),  # type: ignore[arg-type]
        wakeups=BrokenRevisits(tmp_path / "revisits.sqlite3"),
    )
    command = FakeMessage(40)

    await panel.bot.handle_status(cast(object, command), OWNER)  # type: ignore[arg-type]

    assert "Wake-ups: unavailable" in command.replies[0]
    assert buttons(command.reply_markups[0]) == [
        STATUS_INITIATIVE_CALLBACK,
        STATUS_SETTINGS_CALLBACK,
    ]

    message = FakeMessage(41)
    panel.panels.set(TelegramControlPanel(OWNER, 41, "status"))
    await panel.bot.handle_status_callback(
        cast(object, message),  # type: ignore[arg-type]
        OWNER,
        f"{STATUS_WAKEUPS_PREFIX}1",
    )
    assert message.edits[-1] == WAKEUPS_UNAVAILABLE_MESSAGE
