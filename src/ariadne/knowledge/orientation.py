"""The current vocabulary available from private knowledge."""

from __future__ import annotations

from typing import TypedDict


class KnowledgeOrientation(TypedDict):
    """Current generated vocabulary supplied to orientation rendering."""

    kinds: dict[str, int]
    collections: list[str]
    tags: dict[str, int]
    relationships: dict[str, int]
