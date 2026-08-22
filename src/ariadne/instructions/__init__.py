"""Iris's instruction documents.

`base.md` replaces Codex's built-in base instructions, `telegram.md` adds the
rules that hold only for this surface, and `grounding.md` is the developer
message. Documents are prose with `{{ placeholder }}` fields; see
docs/research/codex-base-instructions.md for what they replace.
"""

import re
from collections.abc import Mapping
from importlib.resources import files

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load(name: str) -> str:
    """Return one instruction document, stripped of surrounding whitespace."""
    return files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()


def fill(document: str, values: Mapping[str, str]) -> str:
    """Substitute every `{{ placeholder }}`, refusing to leave one unfilled."""
    missing = set(PLACEHOLDER.findall(document)) - values.keys()
    if missing:
        raise KeyError(f"Missing instruction values: {', '.join(sorted(missing))}")
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], document)


def render(name: str, **values: str) -> str:
    """Load one instruction document and fill in its placeholders."""
    return fill(load(name), values)
