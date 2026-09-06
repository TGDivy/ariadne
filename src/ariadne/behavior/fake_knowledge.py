"""Disposable knowledge used only by manual behaviour scenarios."""

from __future__ import annotations

import json
import os
import re
from functools import wraps
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ariadne.knowledge.paths import slug, title_collision_message
from ariadne.mcp.knowledge import archive_knowledge as real_archive_knowledge
from ariadne.mcp.knowledge import create_knowledge as real_create_knowledge
from ariadne.mcp.knowledge import list_knowledge as real_list_knowledge
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


def _links(
    identifier: str, records: dict[str, dict[str, Any]]
) -> list[dict[str, object]]:
    record = records[identifier]
    ids = set(record.get("links", []))
    ids.update(
        source["id"]
        for source in records.values()
        if identifier in source.get("links", [])
    )
    return [
        {
            "id": target["id"],
            "title": target["title"],
            "summary": target["summary"],
            "folder": target.get("folder", ""),
        }
        for target_id in sorted(ids)
        if (target := records.get(target_id)) is not None and not target.get("archived")
    ]


def _public(
    record: dict[str, Any], records: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "id",
            "title",
            "summary",
            "aliases",
            "folder",
            "archived",
            "body",
        )
    } | {"links": _links(record["id"], records)}


@wraps(real_search_knowledge)
def search_knowledge(
    query: str,
    folder: str | None = None,
    include_archived: bool = False,
    limit: int = 10,
) -> dict[str, object]:
    arguments = {
        "query": query,
        "folder": folder,
        "include_archived": include_archived,
        "limit": limit,
    }
    record_call("search_knowledge", arguments)
    terms = tuple(re.findall(r"[^\W_]+", query.casefold()))
    if not terms:
        raise ToolError("Knowledge search needs at least one searchable word.")
    _, records = _state()
    matches: list[tuple[int, dict[str, object]]] = []
    for record in records.values():
        if record.get("archived") and not include_archived:
            continue
        record_folder = str(record.get("folder", ""))
        if folder not in (None, "") and not (
            record_folder == folder or record_folder.startswith(f"{folder}/")
        ):
            continue
        searchable = " ".join(
            str(record.get(field, ""))
            for field in ("id", "title", "summary", "aliases", "folder", "body")
        ).casefold()
        matched = [term for term in terms if term in searchable]
        if not matched:
            continue
        exact = query.casefold().strip() in {
            str(record.get("id", "")).casefold(),
            str(record.get("title", "")).casefold(),
            *(str(alias).casefold() for alias in record.get("aliases", [])),
        }
        payload = {
            key: record.get(key)
            for key in (
                "id",
                "title",
                "summary",
                "aliases",
                "folder",
                "archived",
            )
        } | {
            "links": _links(record["id"], records),
            "excerpt": str(record.get("body", ""))[:240],
            "matched_terms": matched,
            "unmatched_terms": [term for term in terms if term not in matched],
            "matched_by": ["exact_title" if exact else "scenario"],
        }
        matches.append((-(10_000 if exact else len(matched)), payload))
    selected = [payload for _, payload in sorted(matches, key=lambda item: item[0])][
        :limit
    ]
    return {"results": selected, "count": len(selected)}


@wraps(real_list_knowledge)
def list_knowledge(
    folder: str = "",
    archived: bool = False,
    limit: int = 50,
) -> dict[str, object]:
    arguments = {"folder": folder, "archived": archived, "limit": limit}
    record_call("list_knowledge", arguments)
    if not 1 <= limit <= 50:
        raise ToolError("Knowledge list limit must be 1 to 50.")
    _, records = _state()
    selected = [
        record
        for record in records.values()
        if bool(record.get("archived")) == archived
    ]
    direct = sorted(
        (record for record in selected if str(record.get("folder", "")) == folder),
        key=lambda record: (str(record["title"]).casefold(), str(record["id"])),
    )
    prefix = f"{folder}/" if folder else ""
    child_counts: dict[str, int] = {}
    for record in selected:
        record_folder = str(record.get("folder", ""))
        if folder and not record_folder.startswith(prefix):
            continue
        remainder = record_folder[len(prefix) :] if prefix else record_folder
        if not remainder:
            continue
        child = remainder.split("/", 1)[0]
        child_folder = f"{prefix}{child}"
        child_counts[child_folder] = child_counts.get(child_folder, 0) + 1
    if folder and not direct and not child_counts:
        state = "archived " if archived else ""
        raise ToolError(f"The {state}knowledge folder {folder!r} does not exist.")
    folders = [
        {"folder": name, "record_count": count}
        for name, count in sorted(child_counts.items())[:limit]
    ]
    listed_records = [
        {"id": record["id"], "title": record["title"]} for record in direct[:limit]
    ]
    return {
        "folder": folder,
        "archived": archived,
        "folders": folders,
        "folder_count": len(child_counts),
        "folders_truncated": len(child_counts) > limit,
        "records": listed_records,
        "record_count": len(direct),
        "records_truncated": len(direct) > limit,
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
    body: str,
    folder: str = "",
    aliases: list[str] | None = None,
    links: list[str] | None = None,
) -> dict[str, object]:
    arguments = {
        "title": title,
        "summary": summary,
        "body": body,
        "folder": folder,
        "aliases": aliases,
        "links": links,
    }
    record_call("create_knowledge", arguments)
    path, records = _state()
    identifier = slug(title)
    occupied = {
        name.casefold()
        for record in records.values()
        for name in (str(record["id"]), *map(str, record.get("aliases", [])))
    }
    if identifier.casefold() in occupied:
        raise ToolError(title_collision_message(title, identifier))
    record = {
        "id": identifier,
        "title": title,
        "summary": summary,
        "folder": folder,
        "aliases": aliases or [],
        "links": links or [],
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
    body: str | None = None,
    folder: str | None = None,
    aliases: list[str] | None = None,
    links: list[str] | None = None,
    clear: list[Literal["aliases", "links"]] | None = None,
) -> dict[str, object]:
    arguments = {
        "id": id,
        "title": title,
        "summary": summary,
        "body": body,
        "folder": folder,
        "aliases": aliases,
        "links": links,
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
        ("body", body),
        ("folder", folder),
        ("aliases", aliases),
        ("links", links),
    ):
        if value is not None:
            record[field] = value
    for field in clear or ():
        record[field] = []
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
    server.tool(list_knowledge, annotations=annotations)
    server.tool(read_knowledge, annotations=annotations)
    server.tool(create_knowledge, annotations=annotations)
    server.tool(update_knowledge, annotations=annotations)
    server.tool(archive_knowledge, annotations=annotations)
