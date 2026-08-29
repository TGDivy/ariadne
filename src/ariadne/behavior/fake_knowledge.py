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

from ariadne.knowledge import KnowledgeRelation, KnowledgeSource
from ariadne.mcp.knowledge import archive_knowledge as real_archive_knowledge
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


def _semantic(value: object) -> object:
    if isinstance(value, (KnowledgeRelation, KnowledgeSource)):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def _next_revision(record: dict[str, Any]) -> str:
    return f"scenario:{int(str(record['revision']).split(':')[-1]) + 1}"


def _match(record: dict[str, Any], query: str) -> bool:
    words = query.casefold().split()
    text = " ".join(
        (
            str(record["id"]),
            str(record["title"]),
            " ".join(record.get("aliases", [])),
            str(record.get("body", "")),
        )
    ).casefold()
    return all(word in text for word in words)


@wraps(real_search_knowledge)
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
    arguments = {
        "query": query,
        "kinds": kinds,
        "states": states,
        "date_from": date_from,
        "date_through": date_through,
        "related_to": related_to,
        "include_archived": include_archived,
        "limit": limit,
    }
    record_call("search_knowledge", arguments)
    if not query.strip() and not any(
        (kinds, states, date_from, date_through, related_to)
    ):
        raise ToolError("Knowledge search needs query text or at least one filter.")
    _, records = _state()
    matches = []
    for record in records.values():
        if query and not _match(record, query):
            continue
        if kinds and record["kind"] not in kinds:
            continue
        if states and record.get("state") not in states:
            continue
        record_start = record.get("starts_at")
        record_end = record.get("ends_at") or record_start
        if (date_from or date_through) and record_start is None:
            continue
        if date_from and record_end and str(record_end)[:10] < date_from:
            continue
        if date_through and record_start and str(record_start)[:10] > date_through:
            continue
        if not include_archived and record.get("state") == "archived":
            continue
        if related_to is not None and not (
            any(
                relation["record"] == related_to
                for relation in record.get("related", [])
            )
            or any(
                source["id"] == related_to
                and any(
                    relation["record"] == record["id"]
                    for relation in source.get("related", [])
                )
                for source in records.values()
            )
        ):
            continue
        matches.append(
            {
                key: record.get(key)
                for key in (
                    "id",
                    "title",
                    "kind",
                    "state",
                    "starts_at",
                    "ends_at",
                    "revision",
                )
            }
            | {
                "related": [
                    relation["record"] for relation in record.get("related", [])
                ],
                "excerpt": str(record.get("body", ""))[:240],
                "matched_by": ["scenario"],
            }
        )
    return {"results": matches[:limit], "count": len(matches[:limit])}


@wraps(real_read_knowledge)
def read_knowledge(ids: list[str]) -> dict[str, object]:
    record_call("read_knowledge", {"ids": ids})
    _, records = _state()
    missing = [identifier for identifier in ids if identifier not in records]
    if missing:
        raise ToolError("Knowledge does not exist: " + ", ".join(missing))
    selected = []
    for identifier in ids:
        record = dict(records[identifier])
        record["incoming"] = [
            {"record": source["id"], "relation": relation["relation"]}
            for source in records.values()
            for relation in source.get("related", [])
            if relation["record"] == identifier
        ]
        selected.append(record)
    return {"records": selected}


@wraps(real_create_knowledge)
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
    arguments = {
        "title": title,
        "kind": kind,
        "body": body,
        "state": state,
        "aliases": aliases,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "related": _semantic(related),
        "sources": _semantic(sources),
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
        "schema": 1,
        "id": identifier,
        "title": title,
        "kind": kind,
        "state": state,
        "aliases": aliases or [],
        "starts_at": starts_at,
        "ends_at": ends_at,
        "related": _semantic(related or []),
        "created_at": "2026-08-29T10:00:00Z",
        "updated_at": "2026-08-29T10:00:00Z",
        "sources": _semantic(sources or []),
        "body": body,
        "revision": "scenario:1",
        "incoming": [],
    }
    records[identifier] = record
    _save(path, records)
    return {"record": record}


@wraps(real_update_knowledge)
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
    arguments = {
        "id": id,
        "expected_revision": expected_revision,
        "title": title,
        "kind": kind,
        "body": body,
        "state": state,
        "aliases": aliases,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "related": _semantic(related),
        "sources": _semantic(sources),
        "clear": clear,
    }
    record_call("update_knowledge", arguments)
    path, records = _state()
    try:
        record = records[id]
    except KeyError as error:
        raise ToolError(f"Knowledge {id!r} does not exist.") from error
    if record["revision"] != expected_revision:
        raise ToolError("This knowledge changed since it was read.")
    for field, value in (
        ("title", title),
        ("kind", kind),
        ("body", body),
        ("state", state),
        ("aliases", aliases),
        ("starts_at", starts_at),
        ("ends_at", ends_at),
        ("related", _semantic(related) if related is not None else None),
    ):
        if value is not None:
            record[field] = value
    for field in clear or ():
        record[field] = [] if field in {"aliases", "related"} else None
    existing_sources = record.get("sources", [])
    assert isinstance(existing_sources, list)
    record["sources"] = existing_sources + [
        source.model_dump(mode="json") for source in sources or ()
    ]
    record["revision"] = _next_revision(record)
    record["updated_at"] = "2026-08-29T10:05:00Z"
    _save(path, records)
    return {"record": record}


@wraps(real_archive_knowledge)
def archive_knowledge(
    id: str, expected_revision: str, reason: str
) -> dict[str, object]:
    record_call(
        "archive_knowledge",
        {"id": id, "expected_revision": expected_revision, "reason": reason},
    )
    path, records = _state()
    try:
        record = records[id]
    except KeyError as error:
        raise ToolError(f"Knowledge {id!r} does not exist.") from error
    if record["revision"] != expected_revision:
        raise ToolError("This knowledge changed since it was read.")
    record["state"] = "archived"
    record["body"] = str(record["body"]) + f"\n\n## Archived\n\n{reason}"
    record["revision"] = _next_revision(record)
    _save(path, records)
    return {"record": record}


def register_tools(server: FastMCP, annotations: dict[str, bool]) -> None:
    """Register every disposable knowledge operation."""
    server.tool(search_knowledge, annotations=annotations)
    server.tool(read_knowledge, annotations=annotations)
    server.tool(create_knowledge, annotations=annotations)
    server.tool(update_knowledge, annotations=annotations)
    server.tool(archive_knowledge, annotations=annotations)
