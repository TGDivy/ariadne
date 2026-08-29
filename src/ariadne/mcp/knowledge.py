"""Semantic MCP capabilities for Ariadne's private knowledge."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from ..knowledge import (
    KnowledgeError,
    KnowledgeRelation,
    KnowledgeSource,
)
from ..knowledge.store import KnowledgeStore

ROOT_ENVIRONMENT = "ARIADNE_KNOWLEDGE_ROOT"
CONTEXT_ENVIRONMENT = "ARIADNE_KNOWLEDGE_CONTEXT"

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


def _trigger_sources() -> tuple[KnowledgeSource, ...]:
    selected = os.environ.get(CONTEXT_ENVIRONMENT)
    if selected is None:
        return ()
    path = Path(selected)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload["sources"]
        if not isinstance(values, list):
            raise TypeError
        return tuple(KnowledgeSource.model_validate(value) for value in values)
    except (
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise ToolError("The current knowledge provenance is unavailable.") from error


def _sources(explicit: list[KnowledgeSource] | None) -> tuple[KnowledgeSource, ...]:
    combined = list(_trigger_sources())
    known = {source.source for source in combined}
    for source in explicit or ():
        if source.source not in known:
            combined.append(source)
            known.add(source.source)
    return tuple(combined)


def search_knowledge(
    query: str = "",
    kinds: list[str] | None = None,
    states: list[str] | None = None,
    date_from: str | None = None,
    date_through: str | None = None,
    related_to: str | None = None,
    include_archived: bool = False,
    limit: int = 10,
) -> dict[str, object]:
    """Find relevant private context by meaning, kind, state, date, or relation.

    Search before creating knowledge or whenever a person, plan, goal, project,
    preference, prior decision, or dated event could materially affect the turn.
    Results are compact candidates; read the useful records before relying on
    them. Archived records are omitted unless explicitly requested.
    """
    try:
        results = _store().search(
            query,
            kinds=kinds or (),
            states=states or (),
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


def read_knowledge(ids: list[str]) -> dict[str, object]:
    """Read up to twenty private knowledge records by stable id.

    Complete records include immediate incoming and outgoing relationships so
    another targeted read can follow context that genuinely matters.
    """
    try:
        records = _store().read(ids)
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"records": [record.public_payload() for record in records]}


def create_knowledge(
    title: str,
    kind: str,
    body: str,
    state: str | None = None,
    aliases: list[str] | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    related: list[KnowledgeRelation] | None = None,
    sources: list[KnowledgeSource] | None = None,
) -> dict[str, object]:
    """Remember new durable or scratch knowledge privately.

    Search first to avoid creating a duplicate canonical record. `kind` is an
    extensible singular concept such as person, plan, goal, project, preference,
    event, or scratch. Routine private remembering is already authorized and
    should not be narrated as an operational update.
    """
    try:
        record = _store().create(
            title=title,
            kind=kind,
            body=body,
            state=state,
            aliases=aliases or (),
            starts_at=starts_at,
            ends_at=ends_at,
            related=related or (),
            sources=_sources(sources),
        )
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"record": record.public_payload()}


def update_knowledge(
    id: str,
    expected_revision: str,
    title: str | None = None,
    kind: str | None = None,
    body: str | None = None,
    state: str | None = None,
    aliases: list[str] | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    related: list[KnowledgeRelation] | None = None,
    sources: list[KnowledgeSource] | None = None,
    clear: list[Literal["state", "aliases", "starts_at", "ends_at", "related"]]
    | None = None,
) -> dict[str, object]:
    """Update one existing private record using its last-read revision.

    Omitted fields remain unchanged. Name optional fields in `clear` to remove
    them. A scratch thought can become settled knowledge by changing its kind.
    If the revision is stale, read the record again and reconsider the change.
    """
    try:
        record = _store().update(
            id,
            expected_revision,
            title=title,
            kind=kind,
            body=body,
            state=state,
            aliases=aliases,
            starts_at=starts_at,
            ends_at=ends_at,
            related=related,
            sources=_sources(sources),
            clear=clear or (),
        )
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"record": record.public_payload()}


def archive_knowledge(
    id: str, expected_revision: str, reason: str
) -> dict[str, object]:
    """Archive stale or superseded private knowledge without deleting history."""
    try:
        record = _store().archive(id, expected_revision, reason)
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
    server.tool(read_knowledge, annotations=read_only)
    server.tool(create_knowledge, annotations=private_write)
    server.tool(update_knowledge, annotations=private_write)
    server.tool(archive_knowledge, annotations=private_write)
