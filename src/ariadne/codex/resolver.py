"""Resolve declarative turn profiles into exact Codex configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..knowledge.capability import ROOT_ENVIRONMENT as KNOWLEDGE_ROOT_ENVIRONMENT
from ..prompts.assembly import assemble_prompts
from .models import CodexTurnSettings, ResolvedTurnProfile, TurnProfile


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
    prompts = assemble_prompts(
        profile,
        human=human,
        personality=personality,
        knowledge_root=knowledge_root,
    )

    values = {
        "ARIADNE_PROFILE": profile.name,
    }
    values.update(
        (name, value)
        for name, value in (mcp_environment or {}).items()
        if name in profile.mcp_environment_names
    )
    if knowledge_root is not None:
        values[KNOWLEDGE_ROOT_ENVIRONMENT] = str(knowledge_root.resolve())

    return ResolvedTurnProfile(
        profile=profile,
        settings=settings or profile.settings,
        cwd=vault,
        base_instruction_sources=prompts.base_sources,
        developer_instruction_sources=prompts.developer_sources,
        base_instructions=prompts.base,
        developer_instructions_core=prompts.developer,
        _mcp_environment_values=tuple(values.items()),
    )
