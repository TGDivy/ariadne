"""Harmless recorded substitutes for capabilities used by behaviour runs."""

from __future__ import annotations

import json
import os
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

STATE_ENVIRONMENT = "ARIADNE_BEHAVIOR_CALLS"


def _record(tool: str, arguments: dict[str, Any]) -> None:
    try:
        path = Path(os.environ[STATE_ENVIRONMENT])
    except KeyError as error:
        raise ToolError("The behaviour run has no call recording path.") from error
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"tool": tool, "arguments": arguments},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


@wraps(real_runtime_status)
def runtime_status() -> dict[str, Any]:
    _record("runtime_status", {})
    return {
        "server": {"name": "ariadne-behaviour", "version": "0.1.0"},
        "scenario": True,
        "capabilities": [
            "runtime_status",
            "send_telegram_message",
            "prepare_files",
            "triage_current_mail",
        ],
    }


@wraps(real_send_telegram_message)
async def send_telegram_message(text: str) -> list[int]:
    if not text.strip():
        raise ToolError("A message needs something to say.")
    _record("send_telegram_message", {"text": text})
    return [1001]


@wraps(real_prepare_files)
async def prepare_files(paths: list[str]) -> dict[str, Any]:
    _record("prepare_files", {"paths": paths})
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
    _record(
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
    server.tool(runtime_status)
    server.tool(send_telegram_message)
    server.tool(prepare_files)
    server.tool(triage_current_mail)
    return server


mcp = create_server()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
