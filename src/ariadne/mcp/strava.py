"""Read-only Strava MCP capabilities."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..strava import (
    StravaAuthorizationRequired,
    StravaClient,
    StravaError,
    StravaTokenState,
)

LOGGER = logging.getLogger(__name__)


def _client() -> StravaClient:
    try:
        client_id = int(os.environ["ARIADNE_STRAVA_CLIENT_ID"])
        client_secret = os.environ["ARIADNE_STRAVA_CLIENT_SECRET"]
        state = Path(os.environ["ARIADNE_STRAVA_STATE"])
    except (KeyError, ValueError) as error:
        raise ToolError("Strava is not configured for this turn.") from error
    return StravaClient(client_id, client_secret, StravaTokenState(state))


def _unix_time(value: str | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolError(f"{field} must be an ISO 8601 date or date-time.") from error
    if parsed.tzinfo is None:
        parsed = datetime.combine(parsed.date(), datetime.min.time(), tzinfo=UTC)
    return int(parsed.timestamp())


def _call(operation: str, **arguments: object) -> dict[str, object]:
    try:
        method = getattr(_client(), operation)
        return cast(dict[str, object], method(**arguments))
    except ToolError:
        raise
    except StravaAuthorizationRequired as error:
        raise ToolError(str(error)) from error
    except StravaError as error:
        raise ToolError(str(error)) from error
    except Exception as error:
        LOGGER.error(
            "Strava operation failed: %s (%s)", operation, type(error).__name__
        )
        raise ToolError("Strava could not complete that operation.") from error


def get_strava_athlete() -> dict[str, object]:
    """Return the connected athlete's minimal identity, without location data."""
    return _call("athlete")


def list_strava_activities(
    after: str | None = None,
    before: str | None = None,
    page: int = 1,
    per_page: int = 30,
) -> dict[str, object]:
    """List private training activities in an optional ISO date/time window.

    Results include training metrics but intentionally omit GPS traces, route
    polylines, and free-form activity descriptions. `activity:read_all` access
    is required because activities can be visible only to their owner.
    """
    if page < 1:
        raise ToolError("page must be at least 1.")
    if not 1 <= per_page <= 200:
        raise ToolError("per_page must be between 1 and 200.")
    after_timestamp = _unix_time(after, "after")
    before_timestamp = _unix_time(before, "before")
    if (
        after_timestamp is not None
        and before_timestamp is not None
        and after_timestamp >= before_timestamp
    ):
        raise ToolError("after must be earlier than before.")
    return _call(
        "activities",
        after=after_timestamp,
        before=before_timestamp,
        page=page,
        per_page=per_page,
    )


def read_strava_activity(activity_id: int) -> dict[str, object]:
    """Read one activity's training metrics, excluding its route and notes."""
    if activity_id <= 0:
        raise ToolError("activity_id must be positive.")
    return _call("activity", activity_id=activity_id)


def get_strava_athlete_stats() -> dict[str, object]:
    """Return the athlete's aggregate run and ride totals from Strava."""
    return _call("athlete_stats")


def register_tools(server: FastMCP) -> None:
    """Register only read-only Strava tools; OAuth stays an operator action."""
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    server.tool(get_strava_athlete, annotations=read_only)
    server.tool(list_strava_activities, annotations=read_only)
    server.tool(read_strava_activity, annotations=read_only)
    server.tool(get_strava_athlete_stats, annotations=read_only)
