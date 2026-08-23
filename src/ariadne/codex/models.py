"""Shared types that describe Codex models and resolved turn profiles."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ReasoningEffort

WebSearchSetting = Literal["disabled", "live"]
ThreadPolicy = Literal["shared", "fresh-per-event"]


@dataclass(frozen=True, slots=True)
class CodexTurnSettings:
    """The model settings that may vary within one turn profile."""

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


@dataclass(frozen=True, slots=True)
class TurnProfile:
    """The complete effective configuration of one kind of Codex turn."""

    name: str
    model: str
    effort: ReasoningEffort
    web_search: WebSearchSetting
    base_instruction_sources: tuple[str, ...]
    developer_instruction_sources: tuple[str, ...]
    base_instructions: str
    developer_instructions_core: str
    enabled_tools: tuple[str, ...]
    thread_policy: ThreadPolicy
    cwd: Path
    sandbox: Sandbox = Sandbox.workspace_write
    approval_mode: ApprovalMode = ApprovalMode.auto_review
    permission_profile: str = "ariadne"
    writable_roots: tuple[Path, ...] = ()
    network_domains: tuple[str, ...] = ()
    allow_local_binding: bool = True
    mcp_environment_names: tuple[str, ...] = ()
    _mcp_environment_values: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def settings(self) -> CodexTurnSettings:
        return CodexTurnSettings(self.model, self.effort, self.web_search)

    @property
    def developer_instructions(self) -> str:
        if self.web_search == "live":
            current_information = """\
## Current information

Live web search is enabled. Use it when current information matters, and include
the actual source links in your final answer when you do."""
        else:
            current_information = """\
## Current information

Live web search is disabled. Do not claim to have searched, researched,
checked, or verified current information on the web."""
        return f"{self.developer_instructions_core}\n\n{current_information}"

    @property
    def mcp_environment_values(self) -> tuple[tuple[str, str], ...]:
        """Return runtime values for MCP construction, never for inspection."""
        return self._mcp_environment_values

    def with_settings(self, settings: CodexTurnSettings) -> TurnProfile:
        """Resolve a dynamic model selection without changing profile identity."""
        return replace(
            self,
            model=settings.model,
            effort=settings.effort,
            web_search=settings.web_search,
        )
