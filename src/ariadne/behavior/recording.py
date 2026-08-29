"""Shared call recording for disposable behaviour capabilities."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError

STATE_ENVIRONMENT = "ARIADNE_BEHAVIOR_CALLS"


def record_call(tool: str, arguments: dict[str, Any]) -> None:
    """Append one harmless scenario capability call."""
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
