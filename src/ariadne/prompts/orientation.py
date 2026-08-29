"""Render current private-knowledge vocabulary into trusted orientation."""

from collections.abc import Iterable, Mapping


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


def render_knowledge_orientation(
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
            "## Current private knowledge",
            "",
            "Current collection structure:",
            "",
            *(tree or ["(empty)"]),
            "",
            _vocabulary("Kinds", kinds),
            _vocabulary("Tags", tags),
            _vocabulary("Relationships", relationships),
        )
    )
