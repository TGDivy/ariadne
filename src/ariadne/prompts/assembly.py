"""Assemble the exact static and generated instructions for one turn profile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..codex.models import TurnProfile
from ..knowledge.store import KnowledgeStore
from . import render


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
        context = KnowledgeStore(root).current_context()
        if context is not None:
            current = f"## Current context\n\n{context.metadata.summary}"
            if context.body:
                current = f"{current}\n\n{context.body}"
            developer_sources += ("generated/current-context",)
            developer = f"{developer}\n\n{current}"
    return PromptAssembly(base_sources, developer_sources, base, developer)
