"""Harmless recorded substitutes for capabilities used by behaviour runs."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ariadne.conversation.models import MessageOrigin, Speaker
from ariadne.mail import Importance, SuggestedAction
from ariadne.mcp.conversation import (
    read_recent_conversation as real_read_recent_conversation,
)
from ariadne.mcp.conversation import share_files as real_share_files
from ariadne.mcp.handoff import (
    hand_off_to_conversation as real_hand_off_to_conversation,
)
from ariadne.mcp.mail import (
    record_current_mail_decision as real_record_current_mail_decision,
)
from ariadne.mcp.revisit import cancel_wakeup as real_cancel_wakeup
from ariadne.mcp.revisit import list_wakeups as real_list_wakeups
from ariadne.mcp.revisit import schedule_wakeup as real_schedule_wakeup
from ariadne.mcp.revisit import update_wakeup as real_update_wakeup
from ariadne.mcp.stewardship import (
    record_stewardship_outcome as real_record_stewardship_outcome,
)
from ariadne.revisit import Attention

from .fake_knowledge import register_tools as register_knowledge_tools
from .recording import STATE_ENVIRONMENT as STATE_ENVIRONMENT
from .recording import record_call


@wraps(real_read_recent_conversation)
def read_recent_conversation(
    since: str,
    before: str | None = None,
    speakers: list[Speaker] | None = None,
    origins: list[MessageOrigin] | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    record_call(
        "read_recent_conversation",
        {
            "since": since,
            "before": before,
            "speakers": speakers,
            "origins": origins,
            "query": query,
            "limit": limit,
        },
    )
    return real_read_recent_conversation(since, before, speakers, origins, query, limit)


@wraps(real_share_files)
def share_files(paths: list[str], caption: str | None = None) -> dict[str, Any]:
    record_call("share_files", {"paths": paths, "caption": caption})
    return {
        "status": "shared",
        "files": [{"filename": Path(path).name, "path": path} for path in paths],
    }


@wraps(real_hand_off_to_conversation)
def hand_off_to_conversation(text: str) -> dict[str, str]:
    if not text.strip():
        raise ToolError("A handoff needs something to say.")
    record_call("hand_off_to_conversation", {"text": text})
    return {
        "status": "staged",
        "handoff_id": "handoff_scenario",
        "activation_key": "scenario",
    }


@wraps(real_record_stewardship_outcome)
def record_stewardship_outcome(
    summary: str,
    broad_attention: str | None = None,
) -> dict[str, str]:
    if not summary.strip():
        raise ToolError("A cycle summary must not be empty.")
    record_call(
        "record_stewardship_outcome",
        {"summary": summary, "broad_attention": broad_attention},
    )
    return {"status": "recorded", "cycle_id": "stewardship_scenario"}


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
    server.tool(read_recent_conversation, annotations=harmless)
    server.tool(share_files, annotations=harmless)
    server.tool(hand_off_to_conversation, annotations=harmless)
    server.tool(record_stewardship_outcome, annotations=harmless)
    server.tool(record_current_mail_decision, annotations=harmless)
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
