"""Render current knowledge vocabulary into trusted model orientation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypedDict


class KnowledgeOrientation(TypedDict):
    """Current generated vocabulary supplied to orientation rendering."""

    kinds: dict[str, int]
    collections: list[str]
    tags: dict[str, int]
    relationships: dict[str, int]


def _tree_lines(collections: Iterable[str]) -> list[str]:
    tree: dict[str, dict[str, object]] = {}
    for collection in sorted(set(collections)):
        node = tree
        for part in collection.split("/"):
            child = node.setdefault(part, {})
            assert isinstance(child, dict)
            node = child  # type: ignore[assignment]

    lines: list[str] = []

    def visit(node: dict[str, dict[str, object]], depth: int) -> None:
        for name, child in sorted(node.items()):
            lines.append(f"{'  ' * depth}{name}/")
            visit(child, depth + 1)  # type: ignore[arg-type]

    visit(tree, 0)
    return lines


def _vocabulary(name: str, values: Mapping[str, int]) -> str:
    rendered = ", ".join(
        f"{value} ({count})" for value, count in sorted(values.items())
    )
    return f"{name}: {rendered or 'none yet'}"


def render_orientation(
    *,
    kinds: Mapping[str, int],
    collections: Iterable[str],
    tags: Mapping[str, int],
    relationships: Mapping[str, int],
) -> str:
    """Render a compact, current map without exposing storage mechanics."""
    tree = _tree_lines(collections)
    return "\n".join(
        (
            "## Private knowledge",
            "",
            "Use search, browse, read, create, update, and archive as ordinary "
            "private memory. Search before creating a canonical record. Reuse "
            "the current vocabulary when it fits; introduce a new term only "
            "when its meaning is genuinely different. Do not narrate routine "
            "knowledge maintenance.",
            "",
            "Current knowledge structure:",
            "",
            *(tree or ["(empty)"]),
            "",
            _vocabulary("Kinds", kinds),
            _vocabulary("Tags", tags),
            _vocabulary("Relationships", relationships),
        )
    )
