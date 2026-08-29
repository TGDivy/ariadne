"""One-off future revisit MCP capability."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..revisit import (
    STATE_ENVIRONMENT,
    Attention,
    RevisitError,
    RevisitState,
    parse_due_at,
)

_STATES: dict[Path, RevisitState] = {}


def _state() -> RevisitState:
    try:
        path = Path(os.environ[STATE_ENVIRONMENT]).resolve()
    except KeyError as error:
        raise ToolError("Future revisits are not configured for this turn.") from error
    state = _STATES.get(path)
    if state is None:
        state = RevisitState(path)
        try:
            # The long-lived revisit runtime owns crash recovery. An MCP process
            # may be serving the revisit currently marked as running.
            state.initialize(recover_running=False)
        except sqlite3.Error as error:
            raise ToolError("Future revisit state could not be opened.") from error
        _STATES[path] = state
    return state


def schedule_wakeup(at: str, note: str, attention: Attention) -> dict[str, object]:
    """Schedule Ariadne to wake you, Iris, once at a future time.

    Write a self-contained `note` saying what to reconsider and why; when the
    time comes you will inspect then-current context before acting.
    Choose the least expensive attention that can reliably do the future work:
    `light` for a predetermined reminder or nudge, `focused` for a bounded review
    using current mail, Calendar, or knowledge, and `deep` for cross-source
    investigation, public research, planning, or meaningful ambiguity. Every
    level has the same capabilities and authority. There is no recurring mode.
    """
    try:
        revisit = _state().schedule(
            due_at=parse_due_at(at), note=note, attention=attention
        )
    except (RevisitError, ValueError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    return {"revisit": revisit.public_payload()}


def list_wakeups() -> dict[str, object]:
    """List your pending, currently running, or failed scheduled wake-ups.

    Completed revisits are intentionally absent. Use the opaque returned ids
    only with `update_wakeup` or `cancel_wakeup`.
    """
    try:
        revisits = _state().list_open()
    except sqlite3.Error as error:
        raise ToolError("Future revisits could not be read.") from error
    return {
        "revisits": [revisit.public_payload() for revisit in revisits],
        "count": len(revisits),
    }


def update_wakeup(
    id: str,
    at: str | None = None,
    note: str | None = None,
    attention: Attention | None = None,
) -> dict[str, object]:
    """Update supplied fields on one pending or failed scheduled wake-up.

    Omitted fields remain unchanged. A changed failed revisit becomes pending
    again. Times must be ISO 8601 timestamps with an explicit timezone offset.
    """
    try:
        revisit = _state().change(
            id,
            due_at=parse_due_at(at) if at is not None else None,
            note=note,
            attention=attention,
        )
    except (RevisitError, ValueError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    return {"revisit": revisit.public_payload()}


def cancel_wakeup(id: str) -> dict[str, str]:
    """Cancel one pending or failed scheduled wake-up by its opaque id."""
    try:
        _state().cancel(id)
    except (RevisitError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    return {"id": id, "status": "cancelled"}


def register_tools(server: FastMCP) -> None:
    """Register future revisit operations with their semantic action hints."""
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
    server.tool(schedule_wakeup, annotations=private_write)
    server.tool(list_wakeups, annotations=read_only)
    server.tool(update_wakeup, annotations=private_write)
    server.tool(cancel_wakeup, annotations=private_write)
