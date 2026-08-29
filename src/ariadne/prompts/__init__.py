"""Prompt documents and small rendering primitives for Iris."""

import re
from collections.abc import Mapping
from importlib.resources import files

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def load(name: str) -> str:
    """Return one prompt document, stripped of surrounding whitespace."""
    return files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()


def fill(document: str, values: Mapping[str, str]) -> str:
    """Substitute every `{{ placeholder }}`, refusing to leave one unfilled."""
    missing = set(PLACEHOLDER.findall(document)) - values.keys()
    if missing:
        raise KeyError(f"Missing prompt values: {', '.join(sorted(missing))}")
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], document)


def render(name: str, **values: str) -> str:
    """Load one prompt document and fill in its placeholders."""
    return fill(load(name), values)


def render_web_search_instructions(enabled: bool) -> str:
    """Describe whether current web information is available in this turn."""
    if enabled:
        return """\
## Current information

Live web search is enabled. Use it when current information matters, and include
the actual source links in your final answer when you do."""
    return """\
## Current information

Live web search is disabled. Do not claim to have searched, researched,
checked, or verified current information on the web."""
