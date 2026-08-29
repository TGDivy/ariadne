"""Resolve declarative turn profiles into exact Codex configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

from ..instructions import fill, render
from ..knowledge.capability import ROOT_ENVIRONMENT as KNOWLEDGE_ROOT_ENVIRONMENT
from ..knowledge.orientation import render_orientation
from ..knowledge.store import KnowledgeStore
from .models import CodexTurnSettings, ResolvedTurnProfile, TurnProfile


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
    personality: Path | None = None,
    settings: CodexTurnSettings | None = None,
    mcp_environment: Mapping[str, str] | None = None,
    knowledge_root: Path | None = None,
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
    if personality is not None:
        personality_text = personality.read_text(encoding="utf-8").strip()
        developer_sources += ("config/personality.md",)
        developer_instructions = (
            f"{developer_instructions}\n\n"
            "## Shared personality and standing preferences\n\n"
            f"{personality_text}"
        )

    values = {
        "ARIADNE_PROFILE": profile.name,
    }
    values.update(
        (name, value)
        for name, value in (mcp_environment or {}).items()
        if name in profile.mcp_environment_names
    )

    resolved = ResolvedTurnProfile(
        profile=profile,
        settings=settings or profile.settings,
        cwd=vault,
        base_instruction_sources=base_sources,
        developer_instruction_sources=developer_sources,
        base_instructions=base_instructions,
        developer_instructions_core=developer_instructions,
        _mcp_environment_values=tuple(values.items()),
    )
    if knowledge_root is not None:
        resolved = with_knowledge_orientation(resolved, knowledge_root)
    return resolved


def with_knowledge_orientation(
    profile: ResolvedTurnProfile, knowledge_root: Path
) -> ResolvedTurnProfile:
    """Attach current private-knowledge vocabulary to a resolved profile."""
    root = knowledge_root.resolve()
    orientation = render_orientation(**KnowledgeStore(root).orientation())
    environment = dict(profile.mcp_environment_values)
    environment[KNOWLEDGE_ROOT_ENVIRONMENT] = str(root)
    return replace(
        profile,
        developer_instruction_sources=(
            *profile.developer_instruction_sources,
            "generated/knowledge-orientation",
        ),
        developer_instructions_core=(
            f"{profile.developer_instructions_core}\n\n{orientation}"
        ),
        _mcp_environment_values=tuple(environment.items()),
    )
