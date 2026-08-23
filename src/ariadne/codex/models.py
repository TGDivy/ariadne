"""Declarations and resolved configuration for Codex turn profiles."""

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
    """The model settings that may be selected for a profile."""

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
    """A declarative profile exported by one conversation surface."""

    name: str
    model: str
    effort: ReasoningEffort
    web_search: WebSearchSetting
    instruction_documents: tuple[str, ...]
    developer_documents: tuple[str, ...]
    enabled_tools: tuple[str, ...]
    thread_policy: ThreadPolicy
    sandbox: Sandbox = Sandbox.workspace_write
    approval_mode: ApprovalMode = ApprovalMode.auto_review
    permission_profile: str = "ariadne"
    writable_roots: tuple[Path, ...] = (Path.home(),)
    network_domains: tuple[str, ...] = ()
    allow_local_binding: bool = True
    mcp_environment_names: tuple[str, ...] = ()

    @property
    def settings(self) -> CodexTurnSettings:
        """Return this profile's default model settings."""
        return CodexTurnSettings(self.model, self.effort, self.web_search)


@dataclass(frozen=True, slots=True)
class ResolvedTurnProfile:
    """The complete effective configuration supplied to Codex."""

    profile: TurnProfile
    settings: CodexTurnSettings
    cwd: Path
    base_instruction_sources: tuple[str, ...]
    developer_instruction_sources: tuple[str, ...]
    base_instructions: str
    developer_instructions_core: str
    _mcp_environment_values: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    @property
    def name(self) -> str:
        return self.profile.name

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def effort(self) -> ReasoningEffort:
        return self.settings.effort

    @property
    def web_search(self) -> WebSearchSetting:
        return self.settings.web_search

    @property
    def enabled_tools(self) -> tuple[str, ...]:
        return self.profile.enabled_tools

    @property
    def thread_policy(self) -> ThreadPolicy:
        return self.profile.thread_policy

    @property
    def sandbox(self) -> Sandbox:
        return self.profile.sandbox

    @property
    def approval_mode(self) -> ApprovalMode:
        return self.profile.approval_mode

    @property
    def permission_profile(self) -> str:
        return self.profile.permission_profile

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return self.profile.writable_roots

    @property
    def network_domains(self) -> tuple[str, ...]:
        return self.profile.network_domains

    @property
    def allow_local_binding(self) -> bool:
        return self.profile.allow_local_binding

    @property
    def mcp_environment_names(self) -> tuple[str, ...]:
        return self.profile.mcp_environment_names

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

    def with_settings(self, settings: CodexTurnSettings) -> ResolvedTurnProfile:
        """Apply a dynamic model selection without changing the declaration."""
        return replace(self, settings=settings)
