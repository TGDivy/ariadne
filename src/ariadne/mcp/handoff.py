"""Background-only capability for staging conversational handoffs."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..handoff import (
    ACTIVATION_KEY_ENVIRONMENT,
    ACTIVATION_SOURCE_ENVIRONMENT,
    STATE_ENVIRONMENT,
    HandoffError,
    HandoffState,
)


def hand_off_to_telegram_conversation(text: str) -> dict[str, str]:
    """Stage free-form context for the continuing Telegram Iris.

    This does not send Telegram prose. The originating job releases the staged
    context only after all of its private work commits successfully. Calling it
    again in the same activation replaces the still-staged context.
    """
    try:
        path = Path(os.environ[STATE_ENVIRONMENT])
        activation_key = os.environ[ACTIVATION_KEY_ENVIRONMENT]
        source = os.environ[ACTIVATION_SOURCE_ENVIRONMENT]
    except KeyError as error:
        raise ToolError(
            "Conversational handoff authority is unavailable in this turn."
        ) from error
    try:
        handoff = HandoffState(path).stage(
            activation_key=activation_key,
            source=source,
            body=text,
        )
    except (HandoffError, OSError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    return {
        "status": "staged",
        "handoff_id": handoff.id,
        "activation_key": handoff.activation_key,
    }


def register_tools(server: FastMCP) -> None:
    """Register the background handoff capability."""
    server.tool(hand_off_to_telegram_conversation)
