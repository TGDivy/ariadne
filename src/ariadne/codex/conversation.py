"""A small in-memory Codex conversation for Ariadne."""

import json
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast

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
    CommandExecutionThreadItem,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    MessagePhase,
    TurnCompletedNotification,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import JsonObject

from .models import CodexModel, CodexTurnSettings, ResolvedTurnProfile

LOGGER = logging.getLogger(__name__)

ActivityCallback = Callable[[str], Awaitable[None]]
SpokenCallback = Callable[[str], None]
StopRequested = Callable[[], bool]

WEB_SEARCH_CONTEXT_SIZE = "medium"
MCP_SERVER_NAME = "ariadne"
TELEGRAM_MESSAGE_TOOL = "send_message"
TELEGRAM_TOOLS = (TELEGRAM_MESSAGE_TOOL, "react")


class TurnInterrupted(Exception):
    """Raised when Codex reports that an active turn was interrupted."""


def _activity_message(item: object) -> str | None:
    """Return a safe, user-facing activity label for a Codex thread item."""
    if isinstance(item, WebSearchThreadItem):
        return "Searching the web…"
    if isinstance(item, McpToolCallThreadItem):
        if item.server == MCP_SERVER_NAME and item.tool in TELEGRAM_TOOLS:
            # Iris is speaking for herself; what she sends is the status.
            return None
        return "Using Ariadne's local capability…"
    if isinstance(item, CommandExecutionThreadItem):
        return "Running a command…"
    if isinstance(item, FileChangeThreadItem):
        return "Editing files…"
    return None


def _spoken_text(item: object) -> str | None:
    """Return the text of a message Iris just sent to Telegram herself."""
    if not isinstance(item, McpToolCallThreadItem):
        return None
    if item.server != MCP_SERVER_NAME or item.tool != TELEGRAM_MESSAGE_TOOL:
        return None
    if item.status != McpToolCallStatus.completed:
        return None

    arguments = item.arguments
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    text = arguments.get("text")
    return text if isinstance(text, str) else None


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
        "mcp_servers.ariadne.args=" + json.dumps(["-m", "ariadne.mcp_server"]),
        "mcp_servers.ariadne.enabled=true",
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
    ) -> None:
        self._profile = profile
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

    async def stream_reply(
        self,
        message: str,
        *,
        image_paths: tuple[Path, ...] = (),
        activity: ActivityCallback | None = None,
        spoken: SpokenCallback | None = None,
        stop_requested: StopRequested | None = None,
    ) -> AsyncIterator[str]:
        """Yield the complete accumulated agent message as each delta arrives.

        `spoken` receives every message Iris sends to Telegram herself during
        the turn, so Ariadne knows what has already reached the chat.
        """
        thread = await self._thread_for_conversation()
        turn = await thread.turn(
            _turn_input(message, image_paths),
            approval_mode=self._profile.approval_mode,
            cwd=str(self._profile.cwd),
            effort=self._profile.effort,
            model=self._profile.model,
            sandbox=self._profile.sandbox,
        )
        self._active_turn = turn

        try:
            if stop_requested is not None and stop_requested():
                await self.interrupt()

            response = ""
            final_answer: str | None = None
            async for event in turn.stream():
                if isinstance(event.payload, AgentMessageDeltaNotification):
                    if event.payload.delta:
                        response += event.payload.delta
                        yield response
                elif isinstance(event.payload, ItemStartedNotification):
                    item = event.payload.item.root
                    activity_message = _activity_message(item)
                    if activity_message is not None and activity is not None:
                        LOGGER.info("Codex activity: %s", activity_message)
                        await activity(activity_message)
                elif isinstance(event.payload, ItemCompletedNotification):
                    item = event.payload.item.root
                    if (
                        isinstance(item, AgentMessageThreadItem)
                        and item.phase == MessagePhase.final_answer
                    ):
                        final_answer = item.text
                    spoken_text = _spoken_text(item)
                    if spoken_text is not None and spoken is not None:
                        spoken(spoken_text)
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

            if final_answer is not None and final_answer != response:
                response = final_answer
                yield response

            if not response:
                raise RuntimeError("Codex completed without an agent response.")
        finally:
            if self._active_turn is turn:
                self._active_turn = None
            if self._interrupting_turn is turn:
                self._interrupting_turn = None
            if self._profile.thread_policy == "fresh-per-event":
                self._thread = None

    async def close(self) -> None:
        """Release the process-wide Codex client during shutdown."""
        await self._client.close()

    def reset(self) -> None:
        """Discard the in-memory thread while retaining The Thread vault."""
        self._thread = None

    async def _thread_for_conversation(self) -> AsyncThread:
        if self._thread is None:
            self._thread = await self._client.thread_start(
                approval_mode=self._profile.approval_mode,
                base_instructions=self._profile.base_instructions,
                config=self._thread_config(),
                cwd=str(self._profile.cwd),
                developer_instructions=self._profile.developer_instructions,
                model=self._profile.model,
                sandbox=self._profile.sandbox,
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
