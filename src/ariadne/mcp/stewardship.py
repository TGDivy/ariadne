"""Turn-scoped Learn record for a stewardship cycle."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..stewardship import (
    CYCLE_ENVIRONMENT,
    STATE_ENVIRONMENT,
    StewardshipState,
    StewardshipStateError,
)


def record_stewardship_outcome(
    summary: str,
    broad_attention: str | None = None,
) -> dict[str, str]:
    """Record the focus, work, and learning from this one creative cycle.

    `broad_attention` is optional. Supply it only when the cycle deliberately
    explored an older or less-active person, place, experience, belief, or
    knowledge neighbourhood, so future cycles can rotate their attention.
    """
    try:
        path = Path(os.environ[STATE_ENVIRONMENT])
        cycle_id = os.environ[CYCLE_ENVIRONMENT]
    except KeyError as error:
        raise ToolError("Stewardship outcome authority is unavailable.") from error
    try:
        StewardshipState(path).record_outcome(
            cycle_id,
            summary=summary,
            broad_attention=broad_attention,
        )
    except (OSError, sqlite3.Error, StewardshipStateError) as error:
        raise ToolError(str(error)) from error
    return {"status": "recorded", "cycle_id": cycle_id}


def register_tools(server: FastMCP) -> None:
    server.tool(record_stewardship_outcome)
