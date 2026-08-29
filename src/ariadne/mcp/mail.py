"""Mail MCP capabilities."""

import logging
import os
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from imapclient import IMAPClient  # type: ignore[import-untyped]

from ..mail import (
    IMAP_HOST,
    Importance,
    MailReader,
    SuggestedAction,
    record_current_mail_decision,
)

LOGGER = logging.getLogger(__name__)


def _with_reader(operation: Callable[[MailReader], dict[str, Any]]) -> dict[str, Any]:
    """Connect for one read-only operation using TOML-derived credentials."""
    try:
        username = os.environ["ARIADNE_MAIL_USERNAME"]
        password = os.environ["ARIADNE_MAIL_APP_PASSWORD"]
    except KeyError as error:
        raise ToolError("Mail reading is not configured for this turn.") from error

    try:
        with IMAPClient(IMAP_HOST, port=993, ssl=True) as client:
            client.login(username, password)
            return operation(MailReader(client))
    except ToolError:
        raise
    except Exception as error:
        LOGGER.error("Mail read failed (%s)", type(error).__name__)
        raise ToolError("Mail could not complete that read.") from error


def search_mail(
    query: str,
    since: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search iCloud Mail using ordinary words, names, companies, or topics.

    `since` and `before`, when useful, are ISO dates (YYYY-MM-DD). Results are
    ranked locally from read-only IMAP metadata and body previews. Use a result
    id with `read_mail` or `read_mail_thread`; no local mailbox copy is kept.
    """
    return _with_reader(
        lambda reader: reader.search(query, since=since, before=before, limit=limit)
    )


def read_mail(id: str) -> dict[str, Any]:
    """Read one message returned by `search_mail`, without marking it read."""
    return _with_reader(lambda reader: reader.read(id))


def read_mail_thread(id: str) -> dict[str, Any]:
    """Read the conversation around one search result without changing mail."""
    return _with_reader(lambda reader: reader.read_thread(id))


def triage_current_mail(
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
        return record_current_mail_decision(
            classification, importance, suggested_action, draft_reply
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


def register_tools(server: FastMCP) -> None:
    """Register mail tools."""
    server.tool(search_mail)
    server.tool(read_mail)
    server.tool(read_mail_thread)
    server.tool(triage_current_mail)
