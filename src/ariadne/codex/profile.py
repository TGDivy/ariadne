"""Resolve declarative turn profiles into exact Codex configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

from ..instructions import fill, render
from .models import CodexTurnSettings, ResolvedTurnProfile, TurnProfile

COMMON_IRIS_TOOLS = ("runtime_status", "send_message", "react", "prepare_files")
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


def _source(profile: TurnProfile, document: str) -> str:
    if document == profile.name:
        return f"ariadne.{profile.name}/instructions.md"
    return f"ariadne.instructions/{document}.md"


def _render_document(
    profile: TurnProfile,
    document: str,
    *,
    human: str,
    repository: Path | None,
) -> str | None:
    if document == "ariadne" and repository is None:
        return None
    if document == profile.name:
        text = (
            files(f"ariadne.{profile.name}")
            .joinpath("instructions.md")
            .read_text(encoding="utf-8")
            .strip()
        )
        return fill(text, {"human": human})
    values = {"human": human}
    if repository is not None:
        values["repo"] = str(repository)
    return render(document, **values)


def _render_documents(
    profile: TurnProfile,
    documents: tuple[str, ...],
    *,
    human: str,
    repository: Path | None,
) -> tuple[tuple[str, ...], str]:
    sources: list[str] = []
    rendered: list[str] = []
    for document in documents:
        text = _render_document(profile, document, human=human, repository=repository)
        if text is not None:
            sources.append(_source(profile, document))
            rendered.append(text)
    return tuple(sources), "\n\n".join(rendered)


def resolve_profile(
    profile: TurnProfile,
    *,
    vault: Path,
    human: str,
    settings: CodexTurnSettings | None = None,
    mcp_environment: Mapping[str, str] | None = None,
) -> ResolvedTurnProfile:
    """Render one exported declaration into the exact turn configuration."""
    repository = _repository_root()
    base_sources, base_instructions = _render_documents(
        profile,
        profile.instruction_documents,
        human=human,
        repository=repository,
    )
    developer_sources, developer_instructions = _render_documents(
        profile,
        profile.developer_documents,
        human=human,
        repository=repository,
    )

    values = {"ARIADNE_VAULT": str(vault)}
    for variable in profile.mcp_environment_names:
        value = os.environ.get(variable)
        if value is not None:
            values[variable] = value
    values.update(mcp_environment or {})

    return ResolvedTurnProfile(
        profile=profile,
        settings=settings or profile.settings,
        cwd=vault,
        base_instruction_sources=base_sources,
        developer_instruction_sources=developer_sources,
        base_instructions=base_instructions,
        developer_instructions_core=developer_instructions,
        _mcp_environment_values=tuple(values.items()),
    )
