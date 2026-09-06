"""Deterministic human-readable names for canonical knowledge records."""

from __future__ import annotations

import re
import unicodedata

_SLUG_CHARACTER = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    """Turn a semantic name into Ariadne's bounded lowercase path form."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    result = _SLUG_CHARACTER.sub("-", normalized.casefold()).strip("-")
    return result[:80].rstrip("-") or "record"


def title_collision_message(title: str, identifier: str) -> str:
    """Ask for a human-recognizable title when its generated id is occupied."""
    return (
        f"The title {title!r} would generate the id {identifier!r}, which is already "
        "used by another knowledge record or alias. Choose a more distinctive "
        "human-readable title, such as a full name, stable context, or relevant "
        "date, then try again."
    )


def filename_matches_title(filename: str, title: str) -> bool:
    """Accept the generated title filename and deterministic collision suffixes."""
    base = slug(title)
    if filename == f"{base}.md":
        return True
    match = re.fullmatch(rf"{re.escape(base)}-([0-9]+)\.md", filename)
    return match is not None and int(match.group(1)) >= 2
