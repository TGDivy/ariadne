"""Calendar MCP capabilities."""

import logging
import os
from collections.abc import Callable
from typing import Any, cast

from caldav.lib.error import AuthorizationError, ETagMismatchError, RateLimitError
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..calendar import (
    CalendarConflict,
    CalendarError,
    CalendarStatus,
    ICloudCalendar,
    InvitationResponse,
    UpdateScope,
)

LOGGER = logging.getLogger(__name__)


def _calendar(operation: str, *args: object, **kwargs: object) -> dict[str, Any]:
    """Connect for one CalDAV operation using TOML-derived credentials."""
    try:
        username = os.environ["ARIADNE_ICLOUD_USERNAME"]
        password = os.environ["ARIADNE_ICLOUD_APP_PASSWORD"]
        timezone = os.environ["ARIADNE_CALENDAR_TIMEZONE"]
    except KeyError as error:
        raise ToolError("iCloud Calendar is not configured for this turn.") from error
    default_calendar = os.environ.get("ARIADNE_CALENDAR_DEFAULT")
    try:
        with ICloudCalendar(
            username,
            password,
            timezone=timezone,
            default_calendar=default_calendar,
        ) as calendar:
            method = cast(Callable[..., dict[str, Any]], getattr(calendar, operation))
            return method(*args, **kwargs)
    except CalendarConflict as error:
        raise ToolError(str(error)) from error
    except CalendarError as error:
        raise ToolError(str(error)) from error
    except AuthorizationError as error:
        raise ToolError(
            "iCloud rejected the configured Calendar credentials."
        ) from error
    except ETagMismatchError as error:
        raise ToolError(
            "The calendar event changed during the write. "
            "Read it again before retrying."
        ) from error
    except RateLimitError as error:
        raise ToolError("iCloud Calendar is temporarily rate limited.") from error
    except Exception as error:
        LOGGER.error(
            "iCloud Calendar operation failed: %s (%s)",
            operation,
            type(error).__name__,
        )
        raise ToolError("iCloud Calendar could not complete that operation.") from error


def list_calendars() -> dict[str, Any]:
    """List the iCloud calendars available to the configured account.

    Returns opaque calendar ids accepted by the other calendar tools, along
    with the configured default and local timezone.
    """
    return _calendar("list_calendars")


def search_calendar_events(
    start: str,
    end: str,
    query: str | None = None,
    calendar_ids: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search iCloud events in a closed ISO date or date-time interval.

    Recurring events are expanded into occurrences. `query` matches event
    titles, descriptions, locations, and attendees. Date-only boundaries use
    the configured calendar timezone.
    """
    return _calendar(
        "search_events",
        start,
        end,
        query=query,
        calendar_ids=calendar_ids,
        limit=limit,
    )


def read_calendar_event(id: str) -> dict[str, Any]:
    """Read one iCloud event or recurrence occurrence returned by search."""
    return _calendar("read_event", id)


def check_calendar_availability(
    start: str, end: str, calendar_ids: list[str] | None = None
) -> dict[str, Any]:
    """Return merged busy intervals across selected iCloud calendars."""
    return _calendar("free_busy", start, end, calendar_ids=calendar_ids)


def create_calendar_event(
    title: str,
    start: str,
    end: str,
    calendar_id: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    recurrence: str | None = None,
    alarms_minutes_before: list[int] | None = None,
    status: CalendarStatus = "confirmed",
    busy: bool = True,
) -> dict[str, Any]:
    """Create an iCloud event, optionally sending invitations to attendees.

    Use date-only `start` and `end` for all-day events; their end is exclusive.
    Timed values are ISO date-times. `timezone` may name their IANA timezone;
    otherwise the configured default is used. `recurrence` is one RFC 5545
    RRULE value.
    """
    return _calendar(
        "create_event",
        title=title,
        start=start,
        end=end,
        calendar_id=calendar_id,
        description=description,
        location=location,
        attendees=attendees,
        timezone=timezone,
        recurrence=recurrence,
        alarms_minutes_before=alarms_minutes_before,
        status=status,
        busy=busy,
    )


def update_calendar_event(
    id: str,
    scope: UpdateScope = "occurrence",
    expected_etag: str | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    recurrence: str | None = None,
    alarms_minutes_before: list[int] | None = None,
    status: CalendarStatus | None = None,
    busy: bool | None = None,
) -> dict[str, Any]:
    """Patch an iCloud event or one recurrence occurrence.

    Omitted fields are unchanged. Empty descriptions, locations, attendee
    lists, alarm lists, or recurrence strings clear those values. Use
    `scope="series"` to change an entire recurring series. Pass the last-read
    ETag when available to reject stale writes.
    """
    return _calendar(
        "update_event",
        id,
        scope=scope,
        expected_etag=expected_etag,
        title=title,
        start=start,
        end=end,
        description=description,
        location=location,
        attendees=attendees,
        timezone=timezone,
        recurrence=recurrence,
        alarms_minutes_before=alarms_minutes_before,
        status=status,
        busy=busy,
    )


def delete_calendar_event(
    id: str,
    scope: UpdateScope = "occurrence",
    expected_etag: str | None = None,
) -> dict[str, Any]:
    """Delete an iCloud event, occurrence, or recurring series immediately."""
    return _calendar("delete_event", id, scope=scope, expected_etag=expected_etag)


def respond_to_calendar_invitation(
    id: str,
    response: InvitationResponse,
    expected_etag: str | None = None,
) -> dict[str, Any]:
    """Accept, tentatively accept, or decline an iCloud calendar invitation."""
    return _calendar(
        "respond_to_invitation",
        id,
        response,
        expected_etag=expected_etag,
    )


def register_tools(server: FastMCP) -> None:
    """Register calendar tools."""
    server.tool(list_calendars)
    server.tool(search_calendar_events)
    server.tool(read_calendar_event)
    server.tool(check_calendar_availability)
    server.tool(create_calendar_event)
    server.tool(update_calendar_event)
    server.tool(delete_calendar_event)
    server.tool(respond_to_calendar_invitation)
