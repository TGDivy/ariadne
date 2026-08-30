"""A small in-memory Codex conversation for Ariadne."""

import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Literal, cast

from openai_codex import (
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    LocalImageInput,
    RunInput,
    TextInput,
)
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CollabAgentToolCallThreadItem,
    CommandExecutionThreadItem,
    ContextCompactedNotification,
    DynamicToolCallThreadItem,
    FileChangeThreadItem,
    ImageGenerationThreadItem,
    ImageViewThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    PlanThreadItem,
    ReasoningSummary,
    ReasoningSummaryTextDeltaNotification,
    ReasoningThreadItem,
    ThreadTokenUsageUpdatedNotification,
    TokenUsageBreakdown,
    TurnCompletedNotification,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import JsonObject

from ..telemetry import Telemetry
from .events import (
    ActivityUpdated,
    AgentMessageCompleted,
    AgentMessageStarted,
    AgentMessageUpdated,
    CapabilityCallCompleted,
    ConversationEvent,
    WorkStarted,
    WorkSummaryUpdated,
)
from .models import CodexModel, CodexTurnSettings, ResolvedTurnProfile

LOGGER = logging.getLogger(__name__)

StopRequested = Callable[[], bool]

WEB_SEARCH_CONTEXT_SIZE = "medium"
MCP_TOOL_TIMEOUT_SECONDS = 16 * 60
MCP_SERVER_NAME = "ariadne"
TELEGRAM_MESSAGE_TOOL = "send_telegram_message"
TELEGRAM_TOOLS = (TELEGRAM_MESSAGE_TOOL, "ask_telegram_question")
MAIL_ACTIVITY = {
    "search_mail": "Searching mail…",
    "read_mail": "Reading mail…",
    "read_mail_thread": "Reading mail thread…",
}
CALENDAR_ACTIVITY = {
    "list_calendars": "Checking calendars…",
    "search_calendar_events": "Searching the calendar…",
    "read_calendar_event": "Reading a calendar event…",
    "check_calendar_availability": "Checking availability…",
    "create_calendar_event": "Creating a calendar event…",
    "update_calendar_event": "Updating a calendar event…",
    "delete_calendar_event": "Deleting a calendar event…",
    "respond_to_calendar_invitation": "Responding to a calendar invitation…",
}
KNOWLEDGE_ACTIVITY = {
    "search_knowledge": "Searching memory…",
    "browse_knowledge": "Browsing memory…",
    "read_knowledge": "Reading memory…",
    "create_knowledge": "Remembering…",
    "update_knowledge": "Updating memory…",
    "archive_knowledge": "Organising memory…",
}
REVISIT_ACTIVITY = {
    "schedule_wakeup": "Scheduling a future wake-up…",
    "list_wakeups": "Checking scheduled wake-ups…",
    "update_wakeup": "Updating a scheduled wake-up…",
    "cancel_wakeup": "Cancelling a scheduled wake-up…",
}
STRAVA_ACTIVITY = {
    "get_strava_athlete": "Checking Strava…",
    "list_strava_activities": "Reading Strava activities…",
    "read_strava_activity": "Reading a Strava activity…",
    "get_strava_athlete_stats": "Reading Strava training totals…",
}
LOCAL_ACTIVITY = {
    "read_recent_telegram_messages": "Reading recent messages…",
    "request_telegram_file_delivery": "Preparing files…",
    "inspect_ariadne_runtime": "Checking Ariadne…",
    "record_current_mail_decision": "Triaging mail…",
}


class TurnInterrupted(Exception):
    """Raised when Codex reports that an active turn was interrupted."""


def _activity_message(item: object) -> str | None:
    """Return a safe, user-facing activity label for a Codex thread item."""
    if isinstance(item, ReasoningThreadItem):
        return "Analysing…"
    if isinstance(item, PlanThreadItem):
        return "Planning…"
    if isinstance(item, WebSearchThreadItem):
        return "Searching the web…"
    if isinstance(item, McpToolCallThreadItem):
        if item.server == MCP_SERVER_NAME and item.tool in TELEGRAM_TOOLS:
            # Iris is speaking for herself; what she sends is the status.
            return None
        if item.server == MCP_SERVER_NAME and item.tool in MAIL_ACTIVITY:
            return MAIL_ACTIVITY[item.tool]
        if item.server == MCP_SERVER_NAME and item.tool in CALENDAR_ACTIVITY:
            return CALENDAR_ACTIVITY[item.tool]
        if item.server == MCP_SERVER_NAME and item.tool in KNOWLEDGE_ACTIVITY:
            return KNOWLEDGE_ACTIVITY[item.tool]
        if item.server == MCP_SERVER_NAME and item.tool in REVISIT_ACTIVITY:
            return REVISIT_ACTIVITY[item.tool]
        if item.server == MCP_SERVER_NAME and item.tool in STRAVA_ACTIVITY:
            return STRAVA_ACTIVITY[item.tool]
        if item.server == MCP_SERVER_NAME and item.tool in LOCAL_ACTIVITY:
            return LOCAL_ACTIVITY[item.tool]
        return "Using Ariadne's local capability…"
    if isinstance(item, CommandExecutionThreadItem):
        return "Running a command…"
    if isinstance(item, FileChangeThreadItem):
        return "Editing files…"
    if isinstance(item, ImageViewThreadItem):
        return "Inspecting an image…"
    if isinstance(item, ImageGenerationThreadItem):
        return "Creating an image…"
    if isinstance(item, DynamicToolCallThreadItem):
        return "Using a capability…"
    if isinstance(item, CollabAgentToolCallThreadItem):
        return "Coordinating work…"
    return None


def _log_mcp_started(item: object, source: str) -> float | None:
    """Log one privacy-safe MCP call boundary and return its start time."""
    if not isinstance(item, McpToolCallThreadItem):
        return None
    LOGGER.info(
        "Codex MCP call started source=%s server=%s tool=%s call_id=%s",
        source,
        item.server,
        item.tool,
        item.id,
    )
    return time.monotonic()


def _log_mcp_finished(item: object, source: str, started_at: float | None) -> None:
    """Log an MCP outcome, including failed Telegram delivery details."""
    if not isinstance(item, McpToolCallThreadItem):
        return
    if item.duration_ms is not None and item.duration_ms > 0:
        duration = item.duration_ms / 1000
    elif started_at is not None:
        duration = time.monotonic() - started_at
    else:
        duration = 0.0
    if item.status == McpToolCallStatus.failed:
        message = item.error.message.lower() if item.error is not None else ""
        if "rejected due to unacceptable risk" in message:
            failure = "policy_rejected"
        elif "connection timed out" in message:
            failure = "connect_timeout"
        elif "timed out while sending" in message:
            failure = "delivery_timeout"
        elif "connection failed" in message:
            failure = "connection"
        elif "not reachable" in message or "not configured" in message:
            failure = "unavailable"
        else:
            failure = "tool_error"
        LOGGER.warning(
            "Codex MCP call finished source=%s server=%s tool=%s call_id=%s "
            "status=%s failure=%s duration=%.2fs",
            source,
            item.server,
            item.tool,
            item.id,
            item.status.value,
            failure,
            duration,
        )
        if item.server == MCP_SERVER_NAME and item.tool == TELEGRAM_MESSAGE_TOOL:
            request = json.dumps(item.arguments, ensure_ascii=False, default=repr)
            error = item.error.message if item.error is not None else "unknown"
            LOGGER.warning(
                "Failed Telegram MCP request call_id=%s request=%s error=%s",
                item.id,
                request,
                json.dumps(error, ensure_ascii=False),
            )
    else:
        LOGGER.info(
            "Codex MCP call finished source=%s server=%s tool=%s call_id=%s "
            "status=%s duration=%.2fs",
            source,
            item.server,
            item.tool,
            item.id,
            item.status.value,
            duration,
        )


def _turn_input(message: str, image_paths: tuple[Path, ...]) -> RunInput:
    """Build Codex turn input from a message and any local image attachments."""
    if not image_paths:
        return message
    return cast(
        RunInput,
        [TextInput(message)] + [LocalImageInput(str(path)) for path in image_paths],
    )


def _sandbox_config_overrides(profile: ResolvedTurnProfile) -> tuple[str, ...]:
    """Return Iris's writable roots and network allowlist.

    The allowlist is enforced by Codex's network proxy, which is off unless
    the feature is enabled.
    """
    domains = ", ".join(
        f'{json.dumps(domain)}="allow"' for domain in profile.network_domains
    )
    writable_roots = ", ".join(
        f'{json.dumps(str(root))}="write"' for root in profile.writable_roots
    )
    permission = f"permissions.{profile.permission_profile}"
    return (
        "features.network_proxy=true",
        f"default_permissions={json.dumps(profile.permission_profile)}",
        f"{permission}.filesystem={{{writable_roots}}}",
        f"{permission}.network.domains={{{domains}}}",
        f"{permission}.network.allow_local_binding="
        f"{str(profile.allow_local_binding).lower()}",
    )


def _mcp_config_overrides(profile: ResolvedTurnProfile) -> tuple[str, ...]:
    """Return the local Ariadne MCP server configuration for Codex."""
    overrides = [
        f"mcp_servers.ariadne.command={json.dumps(sys.executable)}",
        "mcp_servers.ariadne.args=" + json.dumps(["-m", "ariadne.mcp"]),
        "mcp_servers.ariadne.enabled=true",
        f"mcp_servers.ariadne.tool_timeout_sec={MCP_TOOL_TIMEOUT_SECONDS}",
        "mcp_servers.ariadne.enabled_tools=" + json.dumps(profile.enabled_tools),
    ]
    overrides.extend(
        f"mcp_servers.ariadne.env.{name}={json.dumps(value)}"
        for name, value in profile.mcp_environment_values
    )
    return tuple(overrides)


class CodexConversation:
    """Reuse one Codex client and one thread for the lifetime of the process."""

    def __init__(
        self,
        profile: ResolvedTurnProfile,
        *,
        client: AsyncCodex | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._profile = profile
        self._telemetry = telemetry or Telemetry()
        self._client = (
            client
            if client is not None
            else AsyncCodex(
                CodexConfig(
                    config_overrides=_sandbox_config_overrides(profile)
                    + _mcp_config_overrides(profile),
                    cwd=str(profile.cwd),
                )
            )
        )
        self._thread: AsyncThread | None = None
        self._thread_token_usage_total: TokenUsageBreakdown | None = None
        self._last_turn_token_usage: TokenUsageBreakdown | None = None
        self._active_turn: AsyncTurnHandle | None = None
        self._interrupting_turn: AsyncTurnHandle | None = None

    @property
    def settings(self) -> CodexTurnSettings:
        """Return the settings that the next turn will use."""
        return self._profile.settings

    @property
    def profile(self) -> ResolvedTurnProfile:
        """Return the complete configuration of the next turn."""
        return self._profile

    @property
    def last_turn_token_usage(self) -> TokenUsageBreakdown | None:
        """Return Codex's latest reported usage for the preceding turn."""
        return self._last_turn_token_usage

    async def available_models(self) -> tuple[CodexModel, ...]:
        """Return the non-hidden models available to the current Codex runtime."""
        response = await self._client.models()
        return tuple(
            CodexModel(
                identifier=model.model,
                display_name=model.display_name,
                default_effort=model.default_reasoning_effort,
                supported_efforts=tuple(
                    option.reasoning_effort
                    for option in model.supported_reasoning_efforts
                ),
            )
            for model in response.data
            if not model.hidden
        )

    def set_settings(self, settings: CodexTurnSettings) -> None:
        """Apply new process-local settings to subsequent turns."""
        self._profile = self._profile.with_settings(settings)
        self.reset()

    async def interrupt(self) -> bool:
        """Ask Codex to interrupt the active turn, if one has started."""
        turn = self._active_turn
        if turn is None:
            return False
        if self._interrupting_turn is turn:
            return True

        self._interrupting_turn = turn
        try:
            await turn.interrupt()
        except Exception:
            if self._interrupting_turn is turn:
                self._interrupting_turn = None
            raise
        return True

    async def steer(
        self,
        message: str,
        *,
        image_paths: tuple[Path, ...] = (),
    ) -> bool:
        """Add a follow-up message to the active turn, if one has started."""
        turn = self._active_turn
        if turn is None:
            return False
        await turn.steer(_turn_input(message, image_paths))
        return True

    async def stream_turn(
        self,
        message: str,
        *,
        image_paths: tuple[Path, ...] = (),
        stop_requested: StopRequested | None = None,
    ) -> AsyncIterator[ConversationEvent]:
        """Yield semantic work and speech events for one Codex turn."""
        observation = self._telemetry.start_turn(
            source=self._profile.name,
            model=self._profile.model,
            reasoning_effort=self._profile.effort.value,
        )
        self._last_turn_token_usage = None
        usage_baseline = self._thread_token_usage_total
        turn: AsyncTurnHandle | None = None
        status: Literal["success", "failure", "cancelled"] = "failure"
        error: BaseException | None = None
        mcp_started_at: dict[str, float] = {}

        try:
            thread = await self._thread_for_conversation()
            turn = await thread.turn(
                _turn_input(message, image_paths),
                approval_mode=self._profile.approval_mode,
                cwd=str(self._profile.cwd),
                effort=self._profile.effort,
                model=self._profile.model,
                sandbox=self._profile.sandbox,
                summary=ReasoningSummary.model_validate(
                    self._profile.reasoning_summary
                ),
            )
            self._active_turn = turn
            if stop_requested is not None and stop_requested():
                await self.interrupt()

            message_phases: dict[str, MessagePhase] = {}
            message_text: dict[str, str] = {}
            summary_text: dict[tuple[str, int], str] = {}
            async for event in turn.stream():
                if isinstance(event.payload, AgentMessageDeltaNotification):
                    if event.payload.delta:
                        phase = message_phases.get(event.payload.item_id)
                        if phase is None:
                            raise RuntimeError(
                                "Codex streamed speech before declaring its phase."
                            )
                        observation.first_response()
                        accumulated = (
                            message_text.get(event.payload.item_id, "")
                            + event.payload.delta
                        )
                        message_text[event.payload.item_id] = accumulated
                        yield AgentMessageUpdated(
                            event.payload.item_id, phase, accumulated
                        )
                elif isinstance(event.payload, ReasoningSummaryTextDeltaNotification):
                    key = (event.payload.item_id, event.payload.summary_index)
                    accumulated = summary_text.get(key, "") + event.payload.delta
                    summary_text[key] = accumulated
                    yield WorkSummaryUpdated(
                        event.payload.item_id,
                        event.payload.summary_index,
                        accumulated,
                    )
                elif isinstance(event.payload, ItemStartedNotification):
                    item = event.payload.item.root
                    observation.tool_started(item)
                    started_at = _log_mcp_started(item, self._profile.name)
                    if started_at is not None:
                        mcp_started_at[item.id] = started_at
                    if isinstance(item, AgentMessageThreadItem):
                        if item.phase is None:
                            raise RuntimeError(
                                "Codex started speech without a supported phase."
                            )
                        message_phases[item.id] = item.phase
                        message_text[item.id] = ""
                        yield AgentMessageStarted(item.id, item.phase)
                    else:
                        activity_message = _activity_message(item)
                        if activity_message is not None:
                            LOGGER.info("Codex activity: %s", activity_message)
                            if isinstance(item, (ReasoningThreadItem, PlanThreadItem)):
                                yield WorkStarted(item.id, activity_message)
                            else:
                                yield ActivityUpdated(activity_message)
                elif isinstance(event.payload, ItemCompletedNotification):
                    item = event.payload.item.root
                    observation.tool_completed(item)
                    if isinstance(item, McpToolCallThreadItem):
                        _log_mcp_finished(
                            item,
                            self._profile.name,
                            mcp_started_at.pop(item.id, None),
                        )
                        yield CapabilityCallCompleted(
                            server=item.server,
                            tool=item.tool,
                            status=item.status.value,
                            error=item.error.message
                            if item.error is not None
                            else None,
                        )
                    if isinstance(item, AgentMessageThreadItem):
                        phase = message_phases.pop(item.id, None)
                        if item.phase is None or phase != item.phase:
                            raise RuntimeError(
                                "Codex completed speech without its declared phase."
                            )
                        message_text.pop(item.id, None)
                        yield AgentMessageCompleted(item.id, phase, item.text)
                elif isinstance(event.payload, ThreadTokenUsageUpdatedNotification):
                    observation.usage(event.payload.token_usage, usage_baseline)
                    self._last_turn_token_usage = event.payload.token_usage.last
                    self._thread_token_usage_total = event.payload.token_usage.total
                elif isinstance(event.payload, ContextCompactedNotification):
                    observation.compacted()
                elif isinstance(event.payload, TurnCompletedNotification):
                    if event.payload.turn.status == TurnStatus.interrupted:
                        raise TurnInterrupted()
                    if event.payload.turn.error is not None:
                        detail = (
                            event.payload.turn.error.message or "Codex turn failed."
                        )
                        raise RuntimeError(detail)
                    if event.payload.turn.status == TurnStatus.failed:
                        raise RuntimeError("Codex turn failed.")

            status = "success"
        except (TurnInterrupted, asyncio.CancelledError) as caught:
            status = "cancelled"
            error = caught
            raise
        except BaseException as caught:
            error = caught
            raise
        finally:
            observation.finish(status, error)
            if turn is not None and self._active_turn is turn:
                self._active_turn = None
            if turn is not None and self._interrupting_turn is turn:
                self._interrupting_turn = None
            if self._profile.thread_policy == "fresh-per-event":
                self.reset()

    async def close(self) -> None:
        """Release the process-wide Codex client during shutdown."""
        await self._client.close()

    def reset(self) -> None:
        """Discard the in-memory thread while retaining private knowledge."""
        self._thread = None
        self._thread_token_usage_total = None

    async def _thread_for_conversation(self) -> AsyncThread:
        if self._thread is None:
            self._thread_token_usage_total = None
            self._thread = await self._client.thread_start(
                approval_mode=self._profile.approval_mode,
                base_instructions=self._profile.base_instructions,
                config=self._thread_config(),
                cwd=str(self._profile.cwd),
                developer_instructions=self._profile.developer_instructions,
                model=self._profile.model,
                sandbox=self._profile.sandbox,
            )
            self._telemetry.thread_started(
                source=self._profile.name,
                model=self._profile.model,
                reasoning_effort=self._profile.effort.value,
            )
        return self._thread

    def _thread_config(self) -> JsonObject:
        config: JsonObject = {
            "model_reasoning_effort": self._profile.effort.value,
            "web_search": self._profile.web_search,
        }
        if self._profile.web_search == "live":
            config["tools"] = {"web_search": {"context_size": WEB_SEARCH_CONTEXT_SIZE}}
        return config
