"""Local MCP capabilities for Ariadne's Codex conversation."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .file_delivery import FileDelivery, FileDeliveryError

LOGGER = logging.getLogger(__name__)
MCP_PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "ariadne", "version": "0.1.0"}


def _result(value: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, sort_keys=True)}],
        "isError": error,
    }


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


def runtime_status() -> dict[str, Any]:
    vault = Path(os.environ.get("ARIADNE_VAULT", Path.cwd())).resolve()
    return {
        "server": SERVER_INFO,
        "cwd": str(Path.cwd()),
        "vault": str(vault),
        "git": _git_status(vault),
        "process": {
            "current": _process(os.getpid()),
            "parent": _process(os.getppid()),
        },
        "capabilities": ["runtime_status", "prepare_files"],
    }


def prepare_files(arguments: dict[str, Any]) -> dict[str, Any]:
    paths = arguments.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return _result({"error": "paths must be a list of strings"}, error=True)
    try:
        approval_id, files = FileDelivery().stage(paths)
    except FileDeliveryError as error:
        return _result({"error": str(error)}, error=True)
    return _result(
        {
            "approval_id": approval_id,
            "expires_in_seconds": 900,
            "approval_command": f"/approve {approval_id}",
            "files": [
                {
                    "path": str(file.path),
                    "filename": file.path.name,
                    "size_bytes": file.size_bytes,
                }
                for file in files
            ],
        }
    )


TOOLS = [
    {
        "name": "runtime_status",
        "description": (
            "Inspect Ariadne's current local runtime and Git workspace. "
            "Secrets are never returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "prepare_files",
        "description": (
            "Stage existing files under the user's home directory for explicit "
            "Telegram approval. This tool does not upload files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                }
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
    },
]


async def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC MCP request."""
    request_id = message.get("id")
    if request_id is None:
        return None
    method = message.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params", {})
        if not isinstance(params, dict):
            result = _result({"error": "tools/call requires parameters"}, error=True)
        elif params.get("name") == "runtime_status":
            result = _result(runtime_status())
        elif params.get("name") == "prepare_files" and isinstance(
            params.get("arguments", {}), dict
        ):
            result = prepare_files(params.get("arguments", {}))
        else:
            result = _result({"error": "Unknown tool or invalid arguments"}, error=True)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


async def serve() -> None:
    """Serve MCP messages over stdio without non-protocol stdout output."""
    for line in sys.stdin:
        try:
            response = await handle_message(json.loads(line))
            if response is not None:
                print(json.dumps(response), flush=True)
        except Exception:
            LOGGER.exception("MCP request failed")


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
