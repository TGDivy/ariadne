"""Semantic MCP capabilities for Ariadne's private knowledge."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..knowledge import KnowledgeError, KnowledgeRelation
from ..knowledge.capability import ROOT_ENVIRONMENT
from ..knowledge.store import KnowledgeStore

_STORES: dict[Path, KnowledgeStore] = {}


def _store() -> KnowledgeStore:
    try:
        root = Path(os.environ[ROOT_ENVIRONMENT]).resolve()
    except KeyError as error:
        raise ToolError("Private knowledge is not configured for this turn.") from error
    try:
        store = _STORES.get(root)
        if store is None:
            store = KnowledgeStore(root)
            _STORES[root] = store
        return store
    except KnowledgeError as error:
        raise ToolError(str(error)) from error


def search_knowledge(
    query: str = "",
    kinds: list[str] | None = None,
    collections: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_through: str | None = None,
    related_to: str | None = None,
    include_archived: bool = False,
    limit: int = 10,
) -> dict[str, object]:
    """Find relevant private context using transparent ranked lexical search.

    Query terms broaden the search rather than all being mandatory. Exact ids,
    titles, aliases, and phrases rank highest, followed by weighted title,
    alias, tag, summary, collection, and body matches. Words use prefix matching
    and stemming, and small spelling differences are tolerated in indexed
    semantic fields. This is not embedding search and does not infer arbitrary
    synonyms. Filters are exact; every requested tag must be present.

    Search before creating knowledge and whenever prior context may materially
    affect the turn. Results include summaries, matching evidence, and compact
    direct relationships. Read useful candidates before relying on them.
    """
    try:
        results = _store().search(
            query,
            kinds=kinds or (),
            collections=collections or (),
            tags=tags or (),
            date_from=date_from,
            date_through=date_through,
            related_to=related_to,
            include_archived=include_archived,
            limit=limit,
        )
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {
        "results": [result.model_dump(mode="json") for result in results],
        "count": len(results),
    }


def browse_knowledge(
    location: str = "",
    depth: int = 2,
    include_summaries: bool = True,
    include_archived: bool = False,
) -> dict[str, object]:
    """Browse the familiar knowledge collection tree at one to five levels.

    Use this when search wording is uncertain, neighbouring records matter, or
    a broader map would help. `location` is a lowercase collection path returned
    by an earlier browse. Records are identified by stable ids; paths are for
    orientation and are never used to read or update content.
    """
    try:
        return _store().browse(
            location,
            depth=depth,
            include_summaries=include_summaries,
            include_archived=include_archived,
        )
    except KnowledgeError as error:
        raise ToolError(str(error)) from error


def read_knowledge(ids: list[str]) -> dict[str, object]:
    """Read up to twenty private records by stable id.

    Full records include compact incoming and outgoing relationship summaries;
    related bodies are read only when their ids are explicitly requested.
    """
    try:
        records = _store().read(ids)
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"records": [record.public_payload() for record in records]}


def create_knowledge(
    title: str,
    summary: str,
    kind: str,
    collection: str,
    body: str,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    related: list[KnowledgeRelation] | None = None,
) -> dict[str, object]:
    """Remember one new canonical private record.

    Search first to avoid duplicates. Choose one primary lowercase `kind`, an
    existing lowercase collection when it fits, and cross-cutting `tags`.
    Ariadne generates the stable id, lowercase kebab-case filename, timestamps,
    Git commit, and synchronization. Routine remembering is already authorized
    and should not be narrated as an operational update.
    """
    try:
        record = _store().create(
            title=title,
            summary=summary,
            kind=kind,
            collection=collection,
            body=body,
            tags=tags or (),
            aliases=aliases or (),
            starts_at=starts_at,
            ends_at=ends_at,
            related=related or (),
        )
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"record": record.public_payload()}


def update_knowledge(
    id: str,
    title: str | None = None,
    summary: str | None = None,
    kind: str | None = None,
    collection: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    related: list[KnowledgeRelation] | None = None,
    clear: list[Literal["tags", "aliases", "starts_at", "ends_at", "related"]]
    | None = None,
) -> dict[str, object]:
    """Update supplied semantic fields on the latest canonical record.

    Omitted fields remain unchanged. Name optional fields in `clear` to remove
    them. Ariadne reads the latest version under a lock, updates its timestamp,
    moves it when its generated location changes, then commits and synchronizes
    everything automatically.
    """
    try:
        record = _store().update(
            id,
            title=title,
            summary=summary,
            kind=kind,
            collection=collection,
            body=body,
            tags=tags,
            aliases=aliases,
            starts_at=starts_at,
            ends_at=ends_at,
            related=related,
            clear=clear or (),
        )
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"record": record.public_payload()}


def archive_knowledge(id: str, reason: str) -> dict[str, object]:
    """Archive stale or superseded private knowledge without deleting it."""
    try:
        record = _store().archive(id, reason)
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"record": record.public_payload()}


def register_tools(server: FastMCP) -> None:
    """Register private knowledge tools with accurate action annotations."""
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    private_write = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    server.tool(search_knowledge, annotations=read_only)
    server.tool(browse_knowledge, annotations=read_only)
    server.tool(read_knowledge, annotations=read_only)
    server.tool(create_knowledge, annotations=private_write)
    server.tool(update_knowledge, annotations=private_write)
    server.tool(archive_knowledge, annotations=private_write)
