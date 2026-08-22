"""Local FastMCP capabilities for Ariadne's Codex conversation."""

import os
import subprocess
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .file_delivery import FileDelivery, FileDeliveryError

mcp = FastMCP(
    "Ariadne",
    instructions="Local runtime inspection and explicitly approved Telegram delivery.",
    version="0.1.0",
    strict_input_validation=True,
)


def _git_status(vault: Path) -> dict[str, Any] | None:
    if not (vault / ".git").exists():
        return None
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
        return None
    return {"root": str(vault), "branch": branch or None, "is_dirty": dirty}


def _process(pid: int) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": pid, "name": None, "parent_pid": None}
    try:
        proc = Path("/proc") / str(pid)
        result["name"] = (proc / "comm").read_text().strip()
        result["parent_pid"] = int((proc / "stat").read_text().split()[3])
    except (OSError, IndexError, ValueError):
        pass
    return result


@mcp.tool
def runtime_status() -> dict[str, Any]:
    """Inspect Ariadne's current local runtime and Git workspace.

    Secrets and environment values are never returned.
    """
    vault = Path(os.environ.get("ARIADNE_VAULT", Path.cwd())).resolve()
    return {
        "server": {"name": "ariadne", "version": "0.1.0"},
        "cwd": str(Path.cwd()),
        "vault": str(vault),
        "git": _git_status(vault),
        "process": {
            "current": _process(os.getpid()),
            "parent": _process(os.getppid()),
        },
        "capabilities": ["runtime_status", "prepare_files"],
    }


@mcp.tool
async def prepare_files(paths: list[str]) -> dict[str, Any]:
    """Stage files under the user's home directory for explicit Telegram approval.

    This tool does not upload files. It sends an explicit Telegram approval card
    with Approve and Reject buttons for the exact staged batch.
    """
    try:
        approval_id, files = FileDelivery().stage(paths)
    except FileDeliveryError as error:
        raise ToolError(str(error)) from error
    try:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
        await FileDelivery().request_approval(
            approval_id, files, token=token, chat_id=chat_id
        )
    except (FileDeliveryError, KeyError, ValueError) as error:
        raise ToolError(str(error)) from error
    return {
        "approval_id": approval_id,
        "expires_in_seconds": 900,
        "approval_requested": True,
        "files": [
            {
                "path": str(file.path),
                "filename": file.path.name,
                "size_bytes": file.size_bytes,
            }
            for file in files
        ],
    }


def main() -> None:
    """Run the local server over FastMCP's default stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
