"""A small in-memory Codex conversation for Ariadne."""

import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    LocalImageInput,
    RunInput,
    Sandbox,
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
    ReasoningEffort,
    TurnCompletedNotification,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import JsonObject

from .instructions import render

LOGGER = logging.getLogger(__name__)

WebSearchSetting = Literal["disabled", "live"]
ActivityCallback = Callable[[str], Awaitable[None]]
SpokenCallback = Callable[[str], None]
StopRequested = Callable[[], bool]

WEB_SEARCH_CONTEXT_SIZE = "medium"
MCP_SERVER_NAME = "ariadne"
MCP_TOOLS = ("runtime_status", "send_message", "react", "prepare_files")
TELEGRAM_MESSAGE_TOOL = "send_message"
MCP_REQUIRED_ENVIRONMENT_VARIABLES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
)

PERMISSION_PROFILE = "ariadne"
NETWORK_DOMAINS = (
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "cdn.playwright.dev",
    "playwright.azureedge.net",
    "localhost",
    "127.0.0.1",
)


@dataclass(frozen=True, slots=True)
class CodexTurnSettings:
    """The explicit settings Ariadne applies to every Codex conversation."""

    model: str
    effort: ReasoningEffort
    web_search: WebSearchSetting


@dataclass(frozen=True, slots=True)
class CodexModel:
    """One model the current Codex runtime says Ariadne may select."""

    identifier: str
    display_name: str
    default_effort: ReasoningEffort
    supported_efforts: tuple[ReasoningEffort, ...]


class TurnInterrupted(Exception):
    """Raised when Codex reports that an active turn was interrupted."""


def _activity_message(item: object) -> str | None:
    """Return a safe, user-facing activity label for a Codex thread item."""
    if isinstance(item, WebSearchThreadItem):
        return "Searching the web…"
    if isinstance(item, McpToolCallThreadItem):
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


def _repository_root() -> Path | None:
    """Return the Ariadne clone this process runs from, when there is one."""
    for directory in Path(__file__).resolve().parents:
        if (directory / ".git").exists():
            return directory
    return None


def _sandbox_config_overrides() -> tuple[str, ...]:
    """Return Iris's writable roots and network allowlist.

    The allowlist is enforced by Codex's network proxy, which is off unless
    the feature is enabled.
    """
    domains = ", ".join(f'{json.dumps(domain)}="allow"' for domain in NETWORK_DOMAINS)
    profile = f"permissions.{PERMISSION_PROFILE}"
    return (
        "features.network_proxy=true",
        f"default_permissions={json.dumps(PERMISSION_PROFILE)}",
        f'{profile}.filesystem={{{json.dumps(str(Path.home()))}="write"}}',
        f"{profile}.network.domains={{{domains}}}",
        f"{profile}.network.allow_local_binding=true",
    )


def _mcp_config_overrides(vault: Path) -> tuple[str, ...]:
    """Return the local Ariadne MCP server configuration for Codex."""
    overrides = [
        f"mcp_servers.ariadne.command={json.dumps(sys.executable)}",
        "mcp_servers.ariadne.args=" + json.dumps(["-m", "ariadne.mcp_server"]),
        "mcp_servers.ariadne.env.ARIADNE_VAULT=" + json.dumps(str(vault)),
        "mcp_servers.ariadne.enabled=true",
        "mcp_servers.ariadne.enabled_tools=" + json.dumps(list(MCP_TOOLS)),
    ]
    for variable in MCP_REQUIRED_ENVIRONMENT_VARIABLES:
        value = os.environ.get(variable)
        if value is not None:
            overrides.append(f"mcp_servers.ariadne.env.{variable}={json.dumps(value)}")
    return tuple(overrides)


class CodexConversation:
    """Reuse one Codex client and one thread for the lifetime of the process."""

    def __init__(
        self,
        vault: Path,
        settings: CodexTurnSettings,
        *,
        human: str,
        client: AsyncCodex | None = None,
    ) -> None:
        self._vault = vault
        self._settings = settings
        self._human = human
        self._client = (
            client
            if client is not None
            else AsyncCodex(
                CodexConfig(
                    config_overrides=_sandbox_config_overrides()
                    + _mcp_config_overrides(vault),
                    cwd=str(vault),
                )
            )
        )
        self._thread: AsyncThread | None = None
        self._active_turn: AsyncTurnHandle | None = None
        self._interrupting_turn: AsyncTurnHandle | None = None

    @property
    def settings(self) -> CodexTurnSettings:
        """Return the settings that the next turn will use."""
        return self._settings

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
        self._settings = settings
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
            approval_mode=ApprovalMode.auto_review,
            cwd=str(self._vault),
            effort=self._settings.effort,
            model=self._settings.model,
            sandbox=Sandbox.workspace_write,
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

    async def close(self) -> None:
        """Release the process-wide Codex client during shutdown."""
        await self._client.close()

    def reset(self) -> None:
        """Discard the in-memory thread while retaining The Thread vault."""
        self._thread = None

    async def _thread_for_conversation(self) -> AsyncThread:
        if self._thread is None:
            self._thread = await self._client.thread_start(
                approval_mode=ApprovalMode.auto_review,
                base_instructions=self._base_instructions(),
                config=self._thread_config(),
                cwd=str(self._vault),
                developer_instructions=self._developer_instructions(),
                model=self._settings.model,
                sandbox=Sandbox.workspace_write,
            )
        return self._thread

    def _base_instructions(self) -> str:
        """Combine the shared instructions with the rules for this surface."""
        return "\n\n".join(
            render(document, human=self._human) for document in ("base", "telegram")
        )

    def _developer_instructions(self) -> str:
        sections = [render("grounding", human=self._human)]
        repository = _repository_root()
        if repository is not None:
            sections.append(render("ariadne", human=self._human, repo=str(repository)))
        instructions = "\n\n".join(sections)
        if self._settings.web_search == "live":
            research_instructions = """\
## Current information

Live web search is enabled. Use it when current information matters, and include
the actual source links in your final answer when you do."""
        else:
            research_instructions = """\
## Current information

Live web search is disabled. Do not claim to have searched, researched,
checked, or verified current information on the web."""
        return f"{instructions}\n\n{research_instructions}"

    def _thread_config(self) -> JsonObject:
        config: JsonObject = {
            "model_reasoning_effort": self._settings.effort.value,
            "web_search": self._settings.web_search,
        }
        if self._settings.web_search == "live":
            config["tools"] = {"web_search": {"context_size": WEB_SEARCH_CONTEXT_SIZE}}
        return config
