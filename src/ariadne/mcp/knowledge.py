"""Small semantic MCP capabilities for Ariadne's private knowledge."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..knowledge import KnowledgeError
from ..knowledge.capability import ROOT_ENVIRONMENT
from ..knowledge.store import KnowledgeStore

_STORES: dict[Path, KnowledgeStore] = {}
_STORES_LOCK = Lock()


def _store() -> KnowledgeStore:
    try:
        root = Path(os.environ[ROOT_ENVIRONMENT]).resolve()
    except KeyError as error:
        raise ToolError("Private knowledge is not configured for this turn.") from error
    try:
        with _STORES_LOCK:
            store = _STORES.get(root)
            if store is None:
                store = KnowledgeStore(root)
                _STORES[root] = store
            return store
    except KnowledgeError as error:
        raise ToolError(str(error)) from error


def search_knowledge(
    query: str,
    include_archived: bool = False,
    limit: int = 10,
) -> dict[str, object]:
    """Find private context using transparent ranked lexical search.

    Use a few concrete names or terms. Exact ids, titles, and aliases rank first,
    followed by weighted title, alias, summary, and body matches. Prefixes,
    stemming, and small spelling differences are supported without pretending to
    infer arbitrary synonyms. Results are compact candidates with excerpts and
    untyped direct links; read only the records that plausibly concern the input.
    Include archived records when looking for completed or superseded history.
    """
    try:
        results = _store().search(
            query,
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
    """Read up to twenty private records by stable id.

    Full records include compact active link and backlink summaries. Linked bodies
    are read only when their ids are explicitly requested.
    """
    try:
        records = _store().read(ids)
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"records": [record.public_payload() for record in records]}


def create_knowledge(
    title: str,
    summary: str,
    body: str,
    aliases: list[str] | None = None,
    links: list[str] | None = None,
) -> dict[str, object]:
    """Remember one new durable subject in private knowledge.

    Search first to avoid duplicates. Write a recognizable title, a compact
    retrieval summary, and one current canonical account. Optional links are
    untyped stable ids; explain their actual meaning in the body. Identity and
    durable storage are handled automatically.
    """
    try:
        record = _store().create(
            title=title,
            summary=summary,
            body=body,
            aliases=aliases or (),
            links=links or (),
        )
    except KnowledgeError as error:
        raise ToolError(str(error)) from error
    return {"record": record.public_payload()}


def update_knowledge(
    id: str,
    title: str | None = None,
    summary: str | None = None,
    body: str | None = None,
    aliases: list[str] | None = None,
    links: list[str] | None = None,
    clear: list[Literal["aliases", "links"]] | None = None,
) -> dict[str, object]:
    """Replace supplied fields on one canonical private record.

    Omitted fields remain unchanged. Bodies are full replacements: reconcile new
    truth into a coherent current account instead of appending a running change
    log. Name aliases or links in `clear` to remove them.
    """
    try:
        record = _store().update(
            id,
            title=title,
            summary=summary,
            body=body,
            aliases=aliases,
            links=links,
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
    server.tool(read_knowledge, annotations=read_only)
    server.tool(create_knowledge, annotations=private_write)
    server.tool(update_knowledge, annotations=private_write)
    server.tool(archive_knowledge, annotations=private_write)
