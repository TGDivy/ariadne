"""Harmless recorded substitutes for capabilities used by behaviour runs."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ariadne.mail import Importance, SuggestedAction
from ariadne.mcp.mail import triage_current_mail as real_triage_current_mail
from ariadne.mcp.runtime import runtime_status as real_runtime_status
from ariadne.mcp.telegram import prepare_files as real_prepare_files
from ariadne.mcp.telegram import send_telegram_message as real_send_telegram_message

from .fake_knowledge import register_tools as register_knowledge_tools
from .recording import STATE_ENVIRONMENT as STATE_ENVIRONMENT
from .recording import record_call


@wraps(real_runtime_status)
def runtime_status() -> dict[str, Any]:
    record_call("runtime_status", {})
    return {
        "server": {"name": "ariadne-behaviour", "version": "0.1.0"},
        "scenario": True,
        "capabilities": [
            "runtime_status",
            "send_telegram_message",
            "prepare_files",
            "triage_current_mail",
            "search_knowledge",
            "read_knowledge",
            "create_knowledge",
            "update_knowledge",
            "archive_knowledge",
        ],
    }


@wraps(real_send_telegram_message)
async def send_telegram_message(text: str) -> list[int]:
    if not text.strip():
        raise ToolError("A message needs something to say.")
    record_call("send_telegram_message", {"text": text})
    return [1001]


@wraps(real_prepare_files)
async def prepare_files(paths: list[str]) -> dict[str, Any]:
    record_call("prepare_files", {"paths": paths})
    return {
        "approval_id": "scenario-approval",
        "expires_in_seconds": 900,
        "approval_requested": True,
        "files": [{"path": path, "filename": Path(path).name} for path in paths],
    }


@wraps(real_triage_current_mail)
def triage_current_mail(
    classification: str,
    importance: Importance,
    suggested_action: SuggestedAction,
    draft_reply: str | None = None,
) -> dict[str, str]:
    record_call(
        "triage_current_mail",
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
    server.tool(runtime_status, annotations=harmless)
    server.tool(send_telegram_message, annotations=harmless)
    server.tool(prepare_files, annotations=harmless)
    server.tool(triage_current_mail, annotations=harmless)
    register_knowledge_tools(server, harmless)
    return server


mcp = create_server()


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
