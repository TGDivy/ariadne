"""Run one behaviour story against real Codex in a disposable workspace."""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from openai_codex import AsyncCodex, CodexConfig

from ariadne.codex import (
    ActivityUpdated,
    AgentMessageCompleted,
    CapabilityCallCompleted,
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
from ariadne.knowledge import KnowledgeMetadata
from ariadne.knowledge.documents import render_document
from ariadne.knowledge.paths import slug
from ariadne.profile import MAIL_PROFILE, TELEGRAM_PROFILE, profile_for_attention
from ariadne.telegram.history import TelegramMessageStore

from .fake_calendar import CALENDAR_ENVIRONMENT
from .fake_knowledge import KNOWLEDGE_ENVIRONMENT
from .fake_mcp import STATE_ENVIRONMENT
from .models import BehaviorScenario

_REDACTED_ENVIRONMENT = (
    "ARIADNE_CONFIG",
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
    "ARIADNE_REVISIT_STATE",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
_SECRET_ENVIRONMENT_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "CREDENTIAL")


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
    duration_seconds: float
    token_usage: dict[str, int] | None
    enabled_capabilities: tuple[str, ...]
    timeline: tuple[TimelineEntry, ...]
    messages: tuple[RecordedMessage, ...]
    capability_attempts: tuple[dict[str, str | None], ...]
    capability_calls: tuple[dict[str, Any], ...]
    calendar_events: tuple[dict[str, Any], ...]
    commits: tuple[str, ...]
    workspace_patch: str
    review_questions: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "web_search": self.web_search,
            "duration_seconds": self.duration_seconds,
            "token_usage": self.token_usage,
            "enabled_capabilities": list(self.enabled_capabilities),
            "timeline": [
                {"kind": entry.kind, "text": entry.text} for entry in self.timeline
            ],
            "messages": [
                {"phase": message.phase, "text": message.text}
                for message in self.messages
            ],
            "capability_attempts": list(self.capability_attempts),
            "capability_calls": list(self.capability_calls),
            "calendar_events": list(self.calendar_events),
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
    (workspace / "archive").mkdir(exist_ok=True)
    for record in scenario.knowledge:
        metadata = KnowledgeMetadata(
            id=record.id,
            title=record.title,
            summary=record.summary,
            aliases=record.aliases,
            links=record.links,
        )
        destination = workspace.joinpath(*record.folder.split("/"))
        destination = destination / f"{slug(record.title)}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(render_document(metadata, record.body))


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


def _fake_mcp_overrides(
    profile_name: str,
    enabled_tools: tuple[str, ...],
    calls: Path,
    knowledge: Path,
    telegram: Path,
) -> tuple[str, ...]:
    return (
        f"mcp_servers.{MCP_SERVER_NAME}.command={json.dumps(sys.executable)}",
        f"mcp_servers.{MCP_SERVER_NAME}.args="
        + json.dumps(["-m", "ariadne.behavior.fake_mcp"]),
        f"mcp_servers.{MCP_SERVER_NAME}.enabled=true",
        f"mcp_servers.{MCP_SERVER_NAME}.tool_timeout_sec={MCP_TOOL_TIMEOUT_SECONDS}",
        f"mcp_servers.{MCP_SERVER_NAME}.enabled_tools=" + json.dumps(enabled_tools),
        f"mcp_servers.{MCP_SERVER_NAME}.env.ARIADNE_PROFILE="
        + json.dumps(profile_name),
        f"mcp_servers.{MCP_SERVER_NAME}.env.{STATE_ENVIRONMENT}="
        + json.dumps(str(calls)),
        f"mcp_servers.{MCP_SERVER_NAME}.env.{KNOWLEDGE_ENVIRONMENT}="
        + json.dumps(str(knowledge)),
        f"mcp_servers.{MCP_SERVER_NAME}.env.TELEGRAM_ALLOWED_USER_ID="
        + json.dumps("7"),
        f"mcp_servers.{MCP_SERVER_NAME}.env.ARIADNE_TELEGRAM_STATE="
        + json.dumps(str(telegram)),
    )


def _read_calls(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    return tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    )


def _read_calendar(path: Path) -> tuple[dict[str, Any], ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(payload["events"])


def _redacted_environment() -> dict[str, str]:
    names = set(_REDACTED_ENVIRONMENT)
    names.update(
        name
        for name in os.environ
        if any(marker in name.upper() for marker in _SECRET_ENVIRONMENT_MARKERS)
    )
    return {name: "" for name in names}


def _write_fake_cli(directory: Path) -> Path:
    """Install the scenario CLI ahead of ordinary commands on PATH."""
    directory.mkdir(mode=0o700)
    executable = directory / "ariadne"
    executable.write_text(
        f"#!{sys.executable}\nfrom ariadne.behavior.fake_cli import main\nmain()\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


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
        knowledge = root / "knowledge.json"
        calendar = root / "calendar.json"
        telegram = root / "telegram.sqlite3"
        scenario_bin = root / "bin"
        workspace.mkdir()
        _write_scenario(scenario, workspace)
        knowledge.write_text(
            json.dumps(
                {"records": [record.payload() for record in scenario.knowledge]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        calendar.write_text(
            json.dumps(
                {
                    "timezone": "Europe/London",
                    "calendars": [
                        {
                            "id": "scenario-calendar",
                            "name": "Personal",
                            "supports_events": True,
                            "is_default": True,
                        }
                    ],
                    "events": [event.payload() for event in scenario.calendar],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        telegram_store = TelegramMessageStore(telegram)
        for item in scenario.telegram:
            telegram_store.record(item.stored(chat_id=7))

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
        _write_fake_cli(scenario_bin)

        if scenario.telegram_prompt is not None:
            surface = TELEGRAM_PROFILE
        elif scenario.revisit is not None:
            surface = profile_for_attention(scenario.revisit.attention)
        else:
            surface = MAIL_PROFILE
        declaration = replace(
            surface,
            writable_roots=(root,),
            network_domains=(),
        )
        profile = resolve_profile(
            declaration,
            vault=workspace,
            human=run_profile.human_name,
            personality=run_profile.personality,
            settings=run_profile.settings,
            knowledge_root=workspace,
        )
        client = AsyncCodex(
            CodexConfig(
                config_overrides=_sandbox_config_overrides(profile)
                + _fake_mcp_overrides(
                    profile.name,
                    profile.enabled_tools,
                    calls,
                    knowledge,
                    telegram,
                ),
                cwd=str(workspace),
                env={
                    **_redacted_environment(),
                    "PATH": f"{scenario_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    STATE_ENVIRONMENT: str(calls),
                    CALENDAR_ENVIRONMENT: str(calendar),
                },
            )
        )
        conversation = CodexConversation(profile, client=client)
        timeline: list[TimelineEntry] = []
        messages: list[RecordedMessage] = []
        capability_attempts: list[dict[str, str | None]] = []
        started_at = time.monotonic()
        try:
            async for event in conversation.stream_turn(
                scenario.turn_input(workspace, human=run_profile.human_name)
            ):
                if isinstance(event, AgentMessageCompleted):
                    messages.append(RecordedMessage(event.phase.value, event.text))
                    entry = TimelineEntry(event.phase.value, event.text)
                elif isinstance(event, (WorkStarted, ActivityUpdated)):
                    text = (
                        event.activity if isinstance(event, WorkStarted) else event.text
                    )
                    entry = TimelineEntry("activity", text)
                elif isinstance(event, CapabilityCallCompleted):
                    capability_attempts.append(
                        {
                            "server": event.server,
                            "tool": event.tool,
                            "status": event.status,
                            "error": event.error,
                        }
                    )
                    suffix = f": {event.error}" if event.error is not None else ""
                    entry = TimelineEntry(
                        "capability",
                        f"{event.tool} {event.status}{suffix}",
                    )
                else:
                    continue
                timeline.append(entry)
                if progress is not None:
                    progress(entry)
        finally:
            await conversation.close()
        duration_seconds = time.monotonic() - started_at
        usage = conversation.last_turn_token_usage

        commits_text = _run_git(
            "log", "--format=%h %s", f"{baseline}..HEAD", cwd=workspace
        )
        return BehaviorReport(
            scenario=scenario.identifier,
            model=profile.model,
            reasoning_effort=profile.effort.value,
            web_search=profile.web_search,
            duration_seconds=duration_seconds,
            token_usage=usage.model_dump() if usage is not None else None,
            enabled_capabilities=(
                *profile.enabled_tools,
                "cli.mail",
                "cli.calendar",
            ),
            timeline=tuple(timeline),
            messages=tuple(messages),
            capability_attempts=tuple(capability_attempts),
            capability_calls=_read_calls(calls),
            calendar_events=_read_calendar(calendar),
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
        f"Duration: `{report.duration_seconds:.1f}s`",
        "Token usage: "
        + (
            ", ".join(
                f"{name.replace('_', ' ')} `{value}`"
                for name, value in report.token_usage.items()
            )
            if report.token_usage is not None
            else "not reported"
        ),
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
    lines.extend(("## Capability attempts", ""))
    if report.capability_attempts:
        lines.extend(
            f"- `{attempt['tool']}`: {attempt['status']}"
            + (f" — {attempt['error']}" if attempt["error"] else "")
            for attempt in report.capability_attempts
        )
    else:
        lines.append("- none")
    lines.extend(("", "## Recorded capability calls", ""))
    if report.capability_calls:
        lines.extend(
            f"- `{call['tool']}`: `{json.dumps(call['arguments'], ensure_ascii=False)}`"
            for call in report.capability_calls
        )
    else:
        lines.append("- none")
    lines.extend(("", "## Calendar after turn", ""))
    if report.calendar_events:
        lines.extend(
            f"- **{event['title']}** — {event['start']} to {event['end']}"
            + (f" — {event['location']}" if event.get("location") else "")
            + (" — free/flexible" if not event.get("busy", True) else "")
            for event in report.calendar_events
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
