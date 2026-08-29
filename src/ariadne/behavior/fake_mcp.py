"""Harmless recorded substitutes for capabilities used by behaviour runs."""

from __future__ import annotations

import os
from functools import wraps
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ariadne.mail import Importance, SuggestedAction
from ariadne.mcp.mail import read_mail as real_read_mail
from ariadne.mcp.mail import read_mail_thread as real_read_mail_thread
from ariadne.mcp.mail import (
    record_current_mail_decision as real_record_current_mail_decision,
)
from ariadne.mcp.mail import search_mail as real_search_mail
from ariadne.mcp.revisit import cancel_wakeup as real_cancel_wakeup
from ariadne.mcp.revisit import list_wakeups as real_list_wakeups
from ariadne.mcp.revisit import schedule_wakeup as real_schedule_wakeup
from ariadne.mcp.revisit import update_wakeup as real_update_wakeup
from ariadne.mcp.runtime import inspect_ariadne_runtime as real_inspect_ariadne_runtime
from ariadne.mcp.telegram import (
    request_telegram_file_delivery as real_request_telegram_file_delivery,
)
from ariadne.mcp.telegram import send_telegram_message as real_send_telegram_message
from ariadne.profile import PROFILES
from ariadne.revisit import Attention

from .fake_calendar import register_tools as register_calendar_tools
from .fake_knowledge import register_tools as register_knowledge_tools
from .recording import STATE_ENVIRONMENT as STATE_ENVIRONMENT
from .recording import record_call


@wraps(real_inspect_ariadne_runtime)
def inspect_ariadne_runtime() -> dict[str, Any]:
    record_call("inspect_ariadne_runtime", {})
    try:
        profile = PROFILES[os.environ["ARIADNE_PROFILE"]]
    except (KeyError, ValueError) as error:
        raise ToolError("Scenario profile is not configured.") from error
    return {
        "server": {"name": "ariadne-behaviour", "version": "0.1.0"},
        "scenario": True,
        "capabilities": list(profile.enabled_tools),
    }


@wraps(real_send_telegram_message)
async def send_telegram_message(text: str) -> list[int]:
    if not text.strip():
        raise ToolError("A message needs something to say.")
    record_call("send_telegram_message", {"text": text})
    return [1001]


@wraps(real_request_telegram_file_delivery)
async def request_telegram_file_delivery(paths: list[str]) -> dict[str, Any]:
    record_call("request_telegram_file_delivery", {"paths": paths})
    return {
        "approval_id": "scenario-approval",
        "expires_in_seconds": 900,
        "approval_requested": True,
        "files": [{"path": path, "filename": Path(path).name} for path in paths],
    }


@wraps(real_record_current_mail_decision)
def record_current_mail_decision(
    classification: str,
    importance: Importance,
    suggested_action: SuggestedAction,
    draft_reply: str | None = None,
) -> dict[str, str]:
    record_call(
        "record_current_mail_decision",
        {
            "classification": classification,
            "importance": importance,
            "suggested_action": suggested_action,
            "draft_reply": draft_reply,
        },
    )
    return {
        "status": "recorded",
        "classification": classification,
        "importance": importance,
        "suggested_action": suggested_action,
    }


@wraps(real_search_mail)
def search_mail(
    query: str,
    since: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    record_call(
        "search_mail",
        {"query": query, "since": since, "before": before, "limit": limit},
    )
    return {"query": query, "results": [], "searched_folders": 1}


@wraps(real_read_mail)
def read_mail(id: str) -> dict[str, Any]:
    record_call("read_mail", {"id": id})
    raise ToolError("That scenario mail id is not available.")


@wraps(real_read_mail_thread)
def read_mail_thread(id: str) -> dict[str, Any]:
    record_call("read_mail_thread", {"id": id})
    raise ToolError("That scenario mail id is not available.")


_REVISITS: dict[str, dict[str, object]] = {}


@wraps(real_schedule_wakeup)
def schedule_wakeup(at: str, note: str, attention: Attention) -> dict[str, object]:
    identifier = f"revisit_scenario_{len(_REVISITS) + 1}"
    revisit: dict[str, object] = {
        "id": identifier,
        "at": at,
        "note": note,
        "attention": attention.value,
        "status": "pending",
    }
    _REVISITS[identifier] = revisit
    record_call(
        "schedule_wakeup",
        {"at": at, "note": note, "attention": attention.value},
    )
    return {"revisit": revisit}


@wraps(real_list_wakeups)
def list_wakeups() -> dict[str, object]:
    record_call("list_wakeups", {})
    revisits = list(_REVISITS.values())
    return {"revisits": revisits, "count": len(revisits)}


@wraps(real_update_wakeup)
def update_wakeup(
    id: str,
    at: str | None = None,
    note: str | None = None,
    attention: Attention | None = None,
) -> dict[str, object]:
    try:
        revisit = _REVISITS[id]
    except KeyError as error:
        raise ToolError(f"Revisit {id!r} does not exist.") from error
    if at is not None:
        revisit["at"] = at
    if note is not None:
        revisit["note"] = note
    if attention is not None:
        revisit["attention"] = attention.value
    record_call(
        "update_wakeup",
        {
            "id": id,
            "at": at,
            "note": note,
            "attention": attention.value if attention is not None else None,
        },
    )
    return {"revisit": revisit}


@wraps(real_cancel_wakeup)
def cancel_wakeup(id: str) -> dict[str, str]:
    if _REVISITS.pop(id, None) is None:
        raise ToolError(f"Revisit {id!r} does not exist.")
    record_call("cancel_wakeup", {"id": id})
    return {"id": id, "status": "cancelled"}


def create_server() -> FastMCP:
    server = FastMCP(
        "Ariadne behaviour scenario",
        instructions="Recorded substitutes for a disposable behaviour run.",
        version="0.1.0",
        strict_input_validation=True,
    )
    harmless = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    server.tool(inspect_ariadne_runtime, annotations=harmless)
    server.tool(send_telegram_message, annotations=harmless)
    server.tool(request_telegram_file_delivery, annotations=harmless)
    server.tool(search_mail, annotations=harmless)
    server.tool(read_mail, annotations=harmless)
    server.tool(read_mail_thread, annotations=harmless)
    server.tool(record_current_mail_decision, annotations=harmless)
    register_calendar_tools(server, harmless)
    register_knowledge_tools(server, harmless)
    server.tool(schedule_wakeup, annotations=harmless)
    server.tool(list_wakeups, annotations=harmless)
    server.tool(update_wakeup, annotations=harmless)
    server.tool(cancel_wakeup, annotations=harmless)
    return server


mcp = create_server()


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
