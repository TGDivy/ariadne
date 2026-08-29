"""Disposable semantic knowledge used only by manual behaviour scenarios."""

from __future__ import annotations

import json
import os
import re
from functools import wraps
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ariadne.knowledge import KnowledgeRelation
from ariadne.mcp.knowledge import archive_knowledge as real_archive_knowledge
from ariadne.mcp.knowledge import browse_knowledge as real_browse_knowledge
from ariadne.mcp.knowledge import create_knowledge as real_create_knowledge
from ariadne.mcp.knowledge import read_knowledge as real_read_knowledge
from ariadne.mcp.knowledge import search_knowledge as real_search_knowledge
from ariadne.mcp.knowledge import update_knowledge as real_update_knowledge

from .recording import record_call

KNOWLEDGE_ENVIRONMENT = "ARIADNE_BEHAVIOR_KNOWLEDGE"


def _state() -> tuple[Path, dict[str, dict[str, Any]]]:
    try:
        path = Path(os.environ[KNOWLEDGE_ENVIRONMENT])
    except KeyError as error:
        raise ToolError("The behaviour run has no knowledge state.") from error
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = {record["id"]: record for record in payload["records"]}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ToolError("The behaviour knowledge state is invalid.") from error
    return path, records


def _save(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.write_text(
        json.dumps({"records": list(records.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _relations(value: list[KnowledgeRelation] | None) -> list[dict[str, str]] | None:
    if value is None:
        return None
    return [relation.model_dump(mode="json") for relation in value]


def _relationships(
    identifier: str, records: dict[str, dict[str, Any]]
) -> list[dict[str, object]]:
    record = records[identifier]
    result = [
        {
            "id": target["id"],
            "title": target["title"],
            "summary": target["summary"],
            "kind": target["kind"],
            "relation": relation["relation"],
            "direction": "outgoing",
        }
        for relation in record.get("related", [])
        if (target := records.get(relation["record"])) is not None
    ]
    result.extend(
        {
            "id": source["id"],
            "title": source["title"],
            "summary": source["summary"],
            "kind": source["kind"],
            "relation": relation["relation"],
            "direction": "incoming",
        }
        for source in records.values()
        for relation in source.get("related", [])
        if relation["record"] == identifier
    )
    return result


def _public(
    record: dict[str, Any], records: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "id",
            "title",
            "summary",
            "kind",
            "collection",
            "tags",
            "aliases",
            "starts_at",
            "ends_at",
            "archived",
            "body",
        )
    } | {"relationships": _relationships(record["id"], records)}


@wraps(real_search_knowledge)
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
    arguments = {
        "query": query,
        "kinds": kinds,
        "collections": collections,
        "tags": tags,
        "date_from": date_from,
        "date_through": date_through,
        "related_to": related_to,
        "include_archived": include_archived,
        "limit": limit,
    }
    record_call("search_knowledge", arguments)
    if not query.strip() and not any(
        (kinds, collections, tags, date_from, date_through, related_to)
    ):
        raise ToolError("Knowledge search needs query text or at least one filter.")
    _, records = _state()
    terms = query.casefold().split()
    matches = []
    for record in records.values():
        searchable = " ".join(
            str(record.get(field, ""))
            for field in (
                "id",
                "title",
                "summary",
                "kind",
                "collection",
                "tags",
                "aliases",
                "body",
            )
        ).casefold()
        matched = [term for term in terms if term in searchable]
        if terms and not matched:
            continue
        if kinds and record["kind"] not in kinds:
            continue
        if collections and record["collection"] not in collections:
            continue
        if tags and not set(tags).issubset(record.get("tags", [])):
            continue
        if not include_archived and record.get("archived"):
            continue
        if related_to is not None and related_to not in {
            relationship["id"] for relationship in _relationships(record["id"], records)
        }:
            continue
        matches.append(
            {
                key: record.get(key)
                for key in (
                    "id",
                    "title",
                    "summary",
                    "kind",
                    "collection",
                    "tags",
                    "starts_at",
                    "ends_at",
                    "archived",
                )
            }
            | {
                "relationships": _relationships(record["id"], records),
                "excerpt": str(record.get("body", ""))[:240],
                "matched_terms": matched,
                "unmatched_terms": [term for term in terms if term not in matched],
                "matched_by": ["scenario"],
            }
        )
    return {"results": matches[:limit], "count": len(matches[:limit])}


@wraps(real_browse_knowledge)
def browse_knowledge(
    location: str = "",
    depth: int = 2,
    include_summaries: bool = True,
    include_archived: bool = False,
) -> dict[str, object]:
    record_call(
        "browse_knowledge",
        {
            "location": location,
            "depth": depth,
            "include_summaries": include_summaries,
            "include_archived": include_archived,
        },
    )
    _, records = _state()
    selected = [
        record
        for record in records.values()
        if (
            not location
            or f"{record['kind']}/{record['collection']}" == location
            or f"{record['kind']}/{record['collection']}".startswith(f"{location}/")
        )
        and (include_archived or not record.get("archived"))
    ]
    return {
        "location": location,
        "records": [
            {
                "id": record["id"],
                "title": record["title"],
                "kind": record["kind"],
                "tags": record.get("tags", []),
                **({"summary": record["summary"]} if include_summaries else {}),
            }
            for record in selected
        ],
    }


@wraps(real_read_knowledge)
def read_knowledge(ids: list[str]) -> dict[str, object]:
    record_call("read_knowledge", {"ids": ids})
    _, records = _state()
    missing = [identifier for identifier in ids if identifier not in records]
    if missing:
        raise ToolError("Knowledge does not exist: " + ", ".join(missing))
    return {"records": [_public(records[identifier], records) for identifier in ids]}


@wraps(real_create_knowledge)
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
    arguments = {
        "title": title,
        "summary": summary,
        "kind": kind,
        "collection": collection,
        "body": body,
        "tags": tags,
        "aliases": aliases,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "related": _relations(related),
    }
    record_call("create_knowledge", arguments)
    path, records = _state()
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    identifier = f"{kind}:{slug}"
    suffix = 2
    while identifier in records:
        identifier = f"{kind}:{slug}-{suffix}"
        suffix += 1
    record = {
        "id": identifier,
        "title": title,
        "summary": summary,
        "kind": kind,
        "collection": collection,
        "tags": tags or [],
        "aliases": aliases or [],
        "starts_at": starts_at,
        "ends_at": ends_at,
        "related": _relations(related) or [],
        "archived": False,
        "body": body,
    }
    records[identifier] = record
    _save(path, records)
    return {"record": _public(record, records)}


@wraps(real_update_knowledge)
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
    arguments = {
        "id": id,
        "title": title,
        "summary": summary,
        "kind": kind,
        "collection": collection,
        "body": body,
        "tags": tags,
        "aliases": aliases,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "related": _relations(related),
        "clear": clear,
    }
    record_call("update_knowledge", arguments)
    path, records = _state()
    try:
        record = records[id]
    except KeyError as error:
        raise ToolError(f"Knowledge {id!r} does not exist.") from error
    for field, value in (
        ("title", title),
        ("summary", summary),
        ("kind", kind),
        ("collection", collection),
        ("body", body),
        ("tags", tags),
        ("aliases", aliases),
        ("starts_at", starts_at),
        ("ends_at", ends_at),
        ("related", _relations(related)),
    ):
        if value is not None:
            record[field] = value
    for field in clear or ():
        record[field] = [] if field in {"tags", "aliases", "related"} else None
    _save(path, records)
    return {"record": _public(record, records)}


@wraps(real_archive_knowledge)
def archive_knowledge(id: str, reason: str) -> dict[str, object]:
    record_call("archive_knowledge", {"id": id, "reason": reason})
    path, records = _state()
    try:
        record = records[id]
    except KeyError as error:
        raise ToolError(f"Knowledge {id!r} does not exist.") from error
    record["archived"] = True
    record["body"] = str(record["body"]) + f"\n\n## Archived\n\n{reason}"
    _save(path, records)
    return {"record": _public(record, records)}


def register_tools(server: FastMCP, annotations: dict[str, bool]) -> None:
    """Register every disposable knowledge operation."""
    server.tool(search_knowledge, annotations=annotations)
    server.tool(browse_knowledge, annotations=annotations)
    server.tool(read_knowledge, annotations=annotations)
    server.tool(create_knowledge, annotations=annotations)
    server.tool(update_knowledge, annotations=annotations)
    server.tool(archive_knowledge, annotations=annotations)
