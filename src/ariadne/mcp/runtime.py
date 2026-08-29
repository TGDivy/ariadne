"""Local runtime inspection MCP capability."""

import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..profile import PROFILES


def _required_environment(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError as error:
        raise ToolError(f"Runtime configuration is missing {name}.") from error


def _process(pid: int) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": pid, "name": None, "parent_pid": None}
    try:
        proc = Path("/proc") / str(pid)
        result["name"] = (proc / "comm").read_text().strip()
        result["parent_pid"] = int((proc / "stat").read_text().split()[3])
    except (OSError, IndexError, ValueError):
        result["inspection"] = "unavailable"
    return result


def inspect_ariadne_runtime() -> dict[str, Any]:
    """Inspect Ariadne's current local runtime.

    Secrets, paths, environment values, and knowledge storage details are never
    returned.
    """
    profile_name = _required_environment("ARIADNE_PROFILE")
    try:
        profile = PROFILES[profile_name]
    except KeyError as error:
        raise ToolError(
            f"Runtime profile {profile_name!r} is not recognized."
        ) from error
    return {
        "server": {"name": "ariadne", "version": "0.1.0"},
        "profile": profile_name,
        "process": {
            "current": _process(os.getpid()),
            "parent": _process(os.getppid()),
        },
        "capabilities": list(profile.enabled_tools),
    }


def register_tools(server: FastMCP) -> None:
    """Register runtime inspection tools."""
    server.tool(inspect_ariadne_runtime)
