"""Run one behaviour story against real Codex in a disposable workspace."""

from __future__ import annotations

import difflib
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from openai_codex import AsyncCodex, CodexConfig

from ariadne.codex import (
    ActivityUpdated,
    AgentMessageCompleted,
    CodexConversation,
    WorkStarted,
)
from ariadne.codex.conversation import (
    MCP_SERVER_NAME,
    MCP_TOOL_TIMEOUT_SECONDS,
    _sandbox_config_overrides,
)
from ariadne.codex.models import CodexTurnSettings
from ariadne.codex.resolver import resolve_profile
from ariadne.profile import MAIL_PROFILE

from .fake_mcp import STATE_ENVIRONMENT
from .models import BehaviorScenario

_REDACTED_ENVIRONMENT = (
    "ARIADNE_CONFIG",
    "ARIADNE_VAULT",
    "ARIADNE_PROFILE",
    "ARIADNE_MAIL_USERNAME",
    "ARIADNE_MAIL_APP_PASSWORD",
    "ARIADNE_MAIL_JOB_ID",
    "ARIADNE_MAIL_STATE",
    "ARIADNE_ICLOUD_USERNAME",
    "ARIADNE_ICLOUD_APP_PASSWORD",
    "ARIADNE_CALENDAR_TIMEZONE",
    "ARIADNE_CALENDAR_DEFAULT",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "ARIADNE_TELEGRAM_STATE",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


@dataclass(frozen=True, slots=True)
class RecordedMessage:
    phase: str
    text: str


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class BehaviorRunProfile:
    """The few local values a model-backed scenario actually needs."""

    human_name: str
    personality: Path | None
    settings: CodexTurnSettings


@dataclass(frozen=True, slots=True)
class BehaviorReport:
    scenario: str
    model: str
    reasoning_effort: str
    web_search: str
    enabled_capabilities: tuple[str, ...]
    timeline: tuple[TimelineEntry, ...]
    messages: tuple[RecordedMessage, ...]
    capability_calls: tuple[dict[str, Any], ...]
    commits: tuple[str, ...]
    workspace_patch: str
    review_questions: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "web_search": self.web_search,
            "enabled_capabilities": list(self.enabled_capabilities),
            "timeline": [
                {"kind": entry.kind, "text": entry.text} for entry in self.timeline
            ],
            "messages": [
                {"phase": message.phase, "text": message.text}
                for message in self.messages
            ],
            "capability_calls": list(self.capability_calls),
            "commits": list(self.commits),
            "workspace_patch": self.workspace_patch,
            "review_questions": list(self.review_questions),
        }


def _run_git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_scenario(scenario: BehaviorScenario, workspace: Path) -> None:
    root = workspace.resolve()
    for fixture in scenario.files:
        destination = (workspace / fixture.path).resolve()
        if not destination.is_relative_to(root):
            raise ValueError(f"Scenario file escapes its workspace: {fixture.path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(fixture.content, encoding="utf-8")


def _snapshot(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(workspace).parts:
            continue
        relative = path.relative_to(workspace).as_posix()
        try:
            snapshot[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            snapshot[relative] = "<binary file>"
    return snapshot


def _snapshot_patch(before: dict[str, str], after: dict[str, str]) -> str:
    chunks: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        old = before.get(path, "").splitlines(keepends=True)
        new = after.get(path, "").splitlines(keepends=True)
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{path}" if path in before else "/dev/null",
                tofile=f"b/{path}" if path in after else "/dev/null",
            )
        )
    return "".join(chunks)


def _fake_mcp_overrides(enabled_tools: tuple[str, ...], calls: Path) -> tuple[str, ...]:
    return (
        f"mcp_servers.{MCP_SERVER_NAME}.command={json.dumps(sys.executable)}",
        f"mcp_servers.{MCP_SERVER_NAME}.args="
        + json.dumps(["-m", "ariadne.behavior.fake_mcp"]),
        f"mcp_servers.{MCP_SERVER_NAME}.enabled=true",
        f"mcp_servers.{MCP_SERVER_NAME}.tool_timeout_sec={MCP_TOOL_TIMEOUT_SECONDS}",
        f"mcp_servers.{MCP_SERVER_NAME}.enabled_tools=" + json.dumps(enabled_tools),
        f"mcp_servers.{MCP_SERVER_NAME}.env.{STATE_ENVIRONMENT}="
        + json.dumps(str(calls)),
    )


def _read_calls(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    return tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    )


async def run_scenario(
    scenario: BehaviorScenario,
    run_profile: BehaviorRunProfile,
    *,
    progress: Callable[[TimelineEntry], None] | None = None,
) -> BehaviorReport:
    """Run a paid, explicitly requested local smoke test and return its evidence."""
    with tempfile.TemporaryDirectory(prefix="ariadne-behaviour-") as temporary:
        root = Path(temporary)
        workspace = root / "thread"
        origin = root / "origin.git"
        calls = root / "capability-calls.jsonl"
        workspace.mkdir()
        _write_scenario(scenario, workspace)

        _run_git("init", "--bare", str(origin), cwd=root)
        _run_git("init", "--initial-branch=main", cwd=workspace)
        _run_git("config", "user.name", "Ariadne Scenario", cwd=workspace)
        _run_git("config", "user.email", "scenario@ariadne.local", cwd=workspace)
        _run_git("remote", "add", "origin", str(origin), cwd=workspace)
        _run_git("add", ".", cwd=workspace)
        _run_git("commit", "-m", "Scenario fixture", cwd=workspace)
        baseline = _run_git("rev-parse", "HEAD", cwd=workspace)
        _run_git("push", "-u", "origin", "main", cwd=workspace)
        before = _snapshot(workspace)

        declaration = replace(
            MAIL_PROFILE,
            writable_roots=(root,),
            network_domains=(),
        )
        profile = resolve_profile(
            declaration,
            vault=workspace,
            human=run_profile.human_name,
            personality=run_profile.personality,
            settings=run_profile.settings,
        )
        client = AsyncCodex(
            CodexConfig(
                config_overrides=_sandbox_config_overrides(profile)
                + _fake_mcp_overrides(profile.enabled_tools, calls),
                cwd=str(workspace),
                env={name: "" for name in _REDACTED_ENVIRONMENT},
            )
        )
        conversation = CodexConversation(profile, client=client)
        timeline: list[TimelineEntry] = []
        messages: list[RecordedMessage] = []
        try:
            async for event in conversation.stream_turn(scenario.turn_input(workspace)):
                if isinstance(event, AgentMessageCompleted):
                    messages.append(RecordedMessage(event.phase.value, event.text))
                    entry = TimelineEntry(event.phase.value, event.text)
                elif isinstance(event, (WorkStarted, ActivityUpdated)):
                    text = (
                        event.activity if isinstance(event, WorkStarted) else event.text
                    )
                    entry = TimelineEntry("activity", text)
                else:
                    continue
                timeline.append(entry)
                if progress is not None:
                    progress(entry)
        finally:
            await conversation.close()

        commits_text = _run_git(
            "log", "--format=%h %s", f"{baseline}..HEAD", cwd=workspace
        )
        return BehaviorReport(
            scenario=scenario.identifier,
            model=profile.model,
            reasoning_effort=profile.effort.value,
            web_search=profile.web_search,
            enabled_capabilities=profile.enabled_tools,
            timeline=tuple(timeline),
            messages=tuple(messages),
            capability_calls=_read_calls(calls),
            commits=tuple(commits_text.splitlines()) if commits_text else (),
            workspace_patch=_snapshot_patch(before, _snapshot(workspace)),
            review_questions=scenario.review_questions,
        )


def render_report(report: BehaviorReport) -> str:
    lines = [
        f"# Behaviour run: {report.scenario}",
        "",
        f"Model: `{report.model}` ({report.reasoning_effort})",
        f"Web search: `{report.web_search}`",
        "Capabilities: "
        + ", ".join(f"`{name}`" for name in report.enabled_capabilities),
        "",
        "## Timeline",
        "",
    ]
    if report.timeline:
        lines.extend(f"- **{entry.kind}:** {entry.text}" for entry in report.timeline)
    else:
        lines.append("- none")
    lines.extend(["", "## Messages", ""])
    if report.messages:
        for message in report.messages:
            lines.extend((f"### {message.phase}", "", message.text, ""))
    else:
        lines.extend(("(none)", ""))
    lines.extend(("## Capability calls", ""))
    if report.capability_calls:
        lines.extend(
            f"- `{call['tool']}`: `{json.dumps(call['arguments'], ensure_ascii=False)}`"
            for call in report.capability_calls
        )
    else:
        lines.append("- none")
    lines.extend(("", "## Commits", ""))
    lines.extend(f"- {commit}" for commit in report.commits)
    if not report.commits:
        lines.append("- none")
    lines.extend(("", "## Workspace changes", ""))
    if report.workspace_patch:
        lines.extend(("```diff", report.workspace_patch.rstrip(), "```"))
    else:
        lines.append("(none)")
    lines.extend(("", "## Review", ""))
    lines.extend(f"- {question}" for question in report.review_questions)
    return "\n".join(lines) + "\n"
