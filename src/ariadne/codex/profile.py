"""Explicit, inspectable configuration for each kind of Codex turn."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

from ..instructions import fill, render
from .models import CodexTurnSettings, ThreadPolicy, TurnProfile

COMMON_IRIS_TOOLS = ("runtime_status", "send_message", "react", "prepare_files")
MAIL_TOOL = "triage_current_mail"
MCP_REQUIRED_ENVIRONMENT_VARIABLES = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
)
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


def _repository_root() -> Path | None:
    for directory in Path(__file__).resolve().parents:
        if (directory / ".git").exists():
            return directory
    return None


def _surface_instructions(package: str, *, human: str) -> str:
    document = (
        files(package).joinpath("instructions.md").read_text(encoding="utf-8").strip()
    )
    return fill(document, {"human": human})


def resolve_profile(
    *,
    name: str,
    surface_package: str,
    vault: Path,
    settings: CodexTurnSettings,
    human: str,
    enabled_tools: tuple[str, ...],
    thread_policy: ThreadPolicy,
    mcp_environment: Mapping[str, str] | None = None,
    mcp_environment_names: tuple[str, ...] = (),
) -> TurnProfile:
    """Render one surface declaration into the exact turn configuration."""
    base_sources = (
        "ariadne.instructions/base.md",
        f"{surface_package}/instructions.md",
    )
    base_instructions = "\n\n".join(
        (
            render("base", human=human),
            _surface_instructions(surface_package, human=human),
        )
    )

    developer_sources = ["ariadne.instructions/grounding.md"]
    developer_sections = [render("grounding", human=human)]
    repository = _repository_root()
    if repository is not None:
        developer_sources.append("ariadne.instructions/ariadne.md")
        developer_sections.append(render("ariadne", human=human, repo=str(repository)))

    explicit_environment = dict(mcp_environment or {})
    values = {"ARIADNE_VAULT": str(vault), **explicit_environment}
    for variable in MCP_REQUIRED_ENVIRONMENT_VARIABLES:
        value = os.environ.get(variable)
        if value is not None:
            values[variable] = value
    environment_names = tuple(
        dict.fromkeys(
            (
                "ARIADNE_VAULT",
                *MCP_REQUIRED_ENVIRONMENT_VARIABLES,
                *mcp_environment_names,
                *explicit_environment,
            )
        )
    )

    return TurnProfile(
        name=name,
        model=settings.model,
        effort=settings.effort,
        web_search=settings.web_search,
        base_instruction_sources=base_sources,
        developer_instruction_sources=tuple(developer_sources),
        base_instructions=base_instructions,
        developer_instructions_core="\n\n".join(developer_sections),
        enabled_tools=enabled_tools,
        thread_policy=thread_policy,
        cwd=vault,
        writable_roots=(Path.home(),),
        network_domains=NETWORK_DOMAINS,
        mcp_environment_names=environment_names,
        _mcp_environment_values=tuple(values.items()),
    )
