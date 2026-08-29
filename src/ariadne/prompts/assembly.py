"""Assemble the exact static and generated instructions for one turn profile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..codex.models import TurnProfile
from ..knowledge.store import KnowledgeStore
from . import render
from .orientation import render_knowledge_orientation


@dataclass(frozen=True, slots=True)
class PromptAssembly:
    """Rendered instruction layers and their inspectable sources."""

    base_sources: tuple[str, ...]
    developer_sources: tuple[str, ...]
    base: str
    developer: str


def _render_documents(
    documents: tuple[str, ...],
    *,
    human: str,
) -> tuple[tuple[str, ...], str]:
    sources = tuple(f"ariadne.prompts/{document}.md" for document in documents)
    rendered = tuple(render(document, human=human) for document in documents)
    return sources, "\n\n".join(rendered)


def assemble_prompts(
    profile: TurnProfile,
    *,
    human: str,
    personality: Path | None = None,
    knowledge_root: Path | None = None,
) -> PromptAssembly:
    """Render every instruction layer that is independent of model settings."""
    base_sources, base = _render_documents(
        profile.instruction_documents,
        human=human,
    )
    developer_sources, developer = _render_documents(
        profile.developer_documents,
        human=human,
    )
    if personality is not None:
        personality_text = personality.read_text(encoding="utf-8").strip()
        developer_sources += ("config/personality.md",)
        developer = (
            f"{developer}\n\n"
            "## Shared personality and standing preferences\n\n"
            f"{personality_text}"
        )
    if knowledge_root is not None:
        root = knowledge_root.resolve()
        orientation = render_knowledge_orientation(**KnowledgeStore(root).orientation())
        developer_sources += ("generated/knowledge-orientation",)
        developer = f"{developer}\n\n{orientation}"
    return PromptAssembly(base_sources, developer_sources, base, developer)
