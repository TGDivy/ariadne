"""Run one bounded creative stewardship cycle per local day."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openai_codex.generated.v2_all import MessagePhase

from ..codex import AgentMessageCompleted, CodexConversation, CodexTurnSettings
from ..codex.resolver import resolve_profile
from ..config import StewardshipSettings
from ..handoff import (
    ACTIVATION_KEY_ENVIRONMENT,
    ACTIVATION_SOURCE_ENVIRONMENT,
    HandoffState,
)
from ..handoff import (
    STATE_ENVIRONMENT as HANDOFF_STATE_ENVIRONMENT,
)
from ..profile import STEWARDSHIP_PROFILE
from ..prompts.activations import build_stewardship_turn_prompt
from ..telemetry import Telemetry
from .models import (
    StewardshipCycle,
    StewardshipRunResult,
    StewardshipRuntimeSnapshot,
    StewardshipSnapshot,
)
from .state import (
    CYCLE_ENVIRONMENT,
    FAILURE_RETRY_SECONDS,
    MAX_ERROR_LENGTH,
    MAX_OUTCOME_LENGTH,
    StewardshipState,
)

LOGGER = logging.getLogger(__name__)

ConversationFactory = Callable[[StewardshipCycle], CodexConversation]


class StewardshipLoop:
    """Schedule and recover the one open-ended daily Iris opportunity."""

    def __init__(
        self,
        settings: StewardshipSettings,
        workspace: Path,
        knowledge_root: Path,
        turn_settings: CodexTurnSettings,
        *,
        human: str,
        personality: Path | None = None,
        mcp_environment: Mapping[str, str] | None = None,
        network_domains: tuple[str, ...] = (),
        telemetry: Telemetry | None = None,
        state: StewardshipState | None = None,
        handoffs: HandoffState | None = None,
        conversation_factory: ConversationFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        recover_running: bool = True,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.knowledge_root = knowledge_root
        self.turn_settings = turn_settings
        self.human = human
        self.personality = personality
        self.mcp_environment = dict(mcp_environment or {})
        self.network_domains = network_domains
        self.telemetry = telemetry or Telemetry()
        self.state = state or StewardshipState(settings.state)
        interrupted = self.state.initialize(recover_running=recover_running)
        handoff_path = self.mcp_environment.get(HANDOFF_STATE_ENVIRONMENT)
        self.handoffs = handoffs or (
            HandoffState(Path(handoff_path)) if handoff_path is not None else None
        )
        if self.handoffs is not None:
            self.handoffs.initialize()
            for cycle_id in interrupted:
                self.handoffs.discard(
                    cycle_id, "Stewardship process stopped before cycle completion"
                )
        self._conversation_factory = conversation_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timezone = ZoneInfo(settings.timezone)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def pause(self, *, until: datetime | None = None) -> StewardshipRuntimeSnapshot:
        """Persistently pause recurrence, optionally until an aware instant."""
        self.state.pause(until=until)
        return self.status_snapshot()

    def resume(self) -> StewardshipRuntimeSnapshot:
        """Resume recurrence without manufacturing a missed-day replay."""
        self.state.resume()
        return self.status_snapshot()

    def status_snapshot(self) -> StewardshipRuntimeSnapshot:
        """Return compact local-time status for operator and Telegram surfaces."""
        now = self._clock()
        snapshot = self.state.snapshot(now=now)
        return StewardshipRuntimeSnapshot(
            enabled=self.settings.enabled,
            timezone=self.settings.timezone,
            waking_start=self.settings.waking_start,
            waking_end=self.settings.waking_end,
            paused=snapshot.paused,
            paused_until=_local(snapshot.paused_until, self._timezone),
            running=snapshot.status == "running",
            active_cycle_id=snapshot.active_cycle_id,
            last_completed_at=_local(snapshot.last_completed_at, self._timezone),
            last_completed_local_day=snapshot.last_completed_local_day,
            next_expected_at=_next_expected_at(
                self.settings,
                snapshot,
                now=now,
                timezone=self._timezone,
            ),
            last_error=snapshot.last_error,
        )

    def _conversation(self, cycle: StewardshipCycle) -> CodexConversation:
        if self._conversation_factory is not None:
            return self._conversation_factory(cycle)
        return CodexConversation(
            resolve_profile(
                STEWARDSHIP_PROFILE,
                workspace=self.workspace,
                settings=self.turn_settings,
                human=self.human,
                personality=self.personality,
                knowledge_root=self.knowledge_root,
                mcp_environment={
                    **self.mcp_environment,
                    CYCLE_ENVIRONMENT: cycle.id,
                    ACTIVATION_KEY_ENVIRONMENT: cycle.id,
                    ACTIVATION_SOURCE_ENVIRONMENT: "stewardship",
                },
                network_domains=self.network_domains,
            ),
            telemetry=self.telemetry,
        )

    async def process_due(self, *, force: bool = False) -> StewardshipRunResult:
        """Run one due cycle and distinguish success, failure, and no claim."""
        if not self.settings.enabled and not force:
            return StewardshipRunResult("not-run")
        awakened_at = self._clock()
        cycle = self.state.claim_due(
            now=awakened_at,
            timezone=self._timezone,
            waking_start=self.settings.waking_start,
            waking_end=self.settings.waking_end,
            force=force,
        )
        if cycle is None:
            return StewardshipRunResult("not-run")
        snapshot = self.state.snapshot()
        conversation: CodexConversation | None = None
        final_summary = "Cycle completed; no native outcome summary was available."
        started_at = time.monotonic()
        try:
            conversation = self._conversation(cycle)
            LOGGER.info(
                "Stewardship cycle started cycle_id=%s local_day=%s model=%s effort=%s",
                cycle.id,
                cycle.local_day,
                conversation.profile.model,
                conversation.profile.effort.value,
            )
            prompt = build_stewardship_turn_prompt(
                cycle=cycle,
                awakened_at=awakened_at,
                timezone=self.settings.timezone,
                waking_start=self.settings.waking_start,
                waking_end=self.settings.waking_end,
                state=snapshot,
            )
            async for event in conversation.stream_turn(prompt):
                if (
                    isinstance(event, AgentMessageCompleted)
                    and event.phase == MessagePhase.final_answer
                    and event.text.strip()
                ):
                    final_summary = event.text.strip()[:MAX_OUTCOME_LENGTH]
        except asyncio.CancelledError:
            if self.handoffs is not None:
                self.handoffs.discard(cycle.id, "Stewardship cycle was cancelled")
            self.state.release(cycle.id)
            self.telemetry.background_job(source="stewardship", status="cancelled")
            raise
        except Exception as error:
            LOGGER.exception("Stewardship cycle failed cycle_id=%s", cycle.id)
            if self.handoffs is not None:
                self.handoffs.discard(cycle.id, str(error))
            self.state.fail(cycle.id, error)
            self.telemetry.background_job(source="stewardship", status="failure")
            result = StewardshipRunResult(
                "failed",
                cycle.id,
                cycle.local_day,
                str(error)[:MAX_ERROR_LENGTH],
            )
        else:
            self.state.complete(cycle.id, fallback_summary=final_summary)
            if self.handoffs is not None:
                self.handoffs.release(cycle.id)
            self.telemetry.background_job(source="stewardship", status="success")
            LOGGER.info(
                "Stewardship cycle completed cycle_id=%s duration=%.2fs",
                cycle.id,
                time.monotonic() - started_at,
            )
            result = StewardshipRunResult("completed", cycle.id, cycle.local_day)
        finally:
            if conversation is not None:
                try:
                    await conversation.close()
                except Exception:
                    LOGGER.exception(
                        "Failed to close stewardship Codex client cycle_id=%s",
                        cycle.id,
                    )
        return result

    async def run_forever(self) -> None:
        """Poll cheaply; state guarantees at most one completed cycle per day."""
        if not self.settings.enabled:
            return
        while not self._stop.is_set():
            try:
                await self.process_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Stewardship source failed; continuing after poll")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.poll_interval_seconds
                )
            except TimeoutError:
                pass


def _next_expected_at(
    settings: StewardshipSettings,
    snapshot: StewardshipSnapshot,
    *,
    now: datetime,
    timezone: ZoneInfo,
) -> datetime | None:
    """Calculate the next eligible local instant without reserving a cycle."""
    if not settings.enabled:
        return None
    if snapshot.paused and snapshot.paused_until is None:
        return None

    floor = now.astimezone(timezone)
    if snapshot.paused_until is not None:
        floor = max(floor, snapshot.paused_until.astimezone(timezone))
    if snapshot.status == "failed" and snapshot.last_attempted_at is not None:
        retry_at = snapshot.last_attempted_at + timedelta(seconds=FAILURE_RETRY_SECONDS)
        floor = max(floor, retry_at.astimezone(timezone))

    blocked_days = {
        day
        for day in (
            snapshot.last_completed_local_day,
            snapshot.active_local_day if snapshot.status == "running" else None,
        )
        if day is not None
    }
    day = floor.date()
    while True:
        start = datetime.combine(day, settings.waking_start, tzinfo=timezone)
        end = datetime.combine(day, settings.waking_end, tzinfo=timezone)
        candidate = max(floor, start)
        if day not in blocked_days and candidate < end:
            return candidate
        day += timedelta(days=1)
        floor = datetime.combine(day, settings.waking_start, tzinfo=timezone)


def _local(value: datetime | None, timezone: ZoneInfo) -> datetime | None:
    return value.astimezone(timezone) if value is not None else None
