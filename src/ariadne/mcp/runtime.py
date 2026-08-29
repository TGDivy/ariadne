"""Local runtime inspection MCP capability."""

import os
import subprocess
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


def _git_status(vault: Path) -> dict[str, Any]:
    if not (vault / ".git").exists():
        return {"root": str(vault), "available": False, "reason": "not_a_repository"}
    try:
        branch = subprocess.run(
            ["git", "-C", str(vault), "branch", "--show-current"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain"],
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return {"root": str(vault), "available": False, "reason": "inspection_failed"}
    return {
        "root": str(vault),
        "available": True,
        "branch": branch or None,
        "is_dirty": dirty,
    }


def _process(pid: int) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": pid, "name": None, "parent_pid": None}
    try:
        proc = Path("/proc") / str(pid)
        result["name"] = (proc / "comm").read_text().strip()
        result["parent_pid"] = int((proc / "stat").read_text().split()[3])
    except (OSError, IndexError, ValueError):
        result["inspection"] = "unavailable"
    return result


def runtime_status() -> dict[str, Any]:
    """Inspect Ariadne's current local runtime and Git workspace.

    Secrets and environment values are never returned.
    """
    vault = Path(_required_environment("ARIADNE_VAULT")).resolve()
    profile_name = _required_environment("ARIADNE_PROFILE")
    try:
        profile = PROFILES[profile_name]
    except KeyError as error:
        raise ToolError(
            f"Runtime profile {profile_name!r} is not recognized."
        ) from error
    return {
        "server": {"name": "ariadne", "version": "0.1.0"},
        "cwd": str(Path.cwd()),
        "vault": str(vault),
        "git": _git_status(vault),
        "process": {
            "current": _process(os.getpid()),
            "parent": _process(os.getppid()),
        },
        "capabilities": list(profile.enabled_tools),
    }


def register_tools(server: FastMCP) -> None:
    """Register runtime inspection tools."""
    server.tool(runtime_status)
