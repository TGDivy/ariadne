"""Private SQLite state shared by the conversation core and its MCP tools."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from ..handoff.state import STATE_ENVIRONMENT

DEFAULT_STATE_PATH = Path("~/.local/state/ariadne/conversation.sqlite3")

__all__ = [
    "DEFAULT_STATE_PATH",
    "STATE_ENVIRONMENT",
    "connect",
    "default_state_path",
    "prepare",
    "state_path",
]


def default_state_path() -> Path:
    """Return the private conversation database used when none is configured."""
    return DEFAULT_STATE_PATH.expanduser()


def state_path(environ: Mapping[str, str] = os.environ) -> Path:
    """Resolve the explicitly forwarded state path or the process default."""
    configured = environ.get(STATE_ENVIRONMENT)
    return Path(configured).expanduser() if configured else default_state_path()


def prepare(path: Path) -> None:
    """Create the owner-only database file and its private parent directory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)


def connect(path: Path) -> sqlite3.Connection:
    """Open one short-lived connection with named-column rows."""
    database = sqlite3.connect(path, timeout=10)
    database.row_factory = sqlite3.Row
    return database
