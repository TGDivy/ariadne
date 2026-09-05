"""Turn-scoped Mail MCP capability."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..mail import Importance, SuggestedAction
from ..mail import (
    record_current_mail_decision as _record_current_mail_decision,
)


def record_current_mail_decision(
    classification: str,
    importance: Importance,
    suggested_action: SuggestedAction,
    draft_reply: str | None = None,
) -> dict[str, str]:
    """Record the decision for this mail event and request a safe mailbox action.

    This capability exists only in mail turns. It can keep or flag the message,
    or move it to one of Ariadne's five configured filing folders. A draft reply
    is recorded for the user; this tool never sends email.
    """
    try:
        return _record_current_mail_decision(
            classification, importance, suggested_action, draft_reply
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


def register_tools(server: FastMCP) -> None:
    """Register the one Mail operation scoped to an active event turn."""
    server.tool(record_current_mail_decision)
