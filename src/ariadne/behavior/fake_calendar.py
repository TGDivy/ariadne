"""Disposable Calendar capability used only by manual behaviour scenarios."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import date, datetime, time
from functools import wraps
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ariadne.calendar import CalendarStatus, InvitationResponse, UpdateScope
from ariadne.mcp.calendar import (
    check_calendar_availability as real_check_calendar_availability,
)
from ariadne.mcp.calendar import create_calendar_event as real_create_calendar_event
from ariadne.mcp.calendar import delete_calendar_event as real_delete_calendar_event
from ariadne.mcp.calendar import list_calendars as real_list_calendars
from ariadne.mcp.calendar import read_calendar_event as real_read_calendar_event
from ariadne.mcp.calendar import (
    respond_to_calendar_invitation as real_respond_to_calendar_invitation,
)
from ariadne.mcp.calendar import search_calendar_events as real_search_calendar_events
from ariadne.mcp.calendar import update_calendar_event as real_update_calendar_event

from .recording import record_call

CALENDAR_ENVIRONMENT = "ARIADNE_BEHAVIOR_CALENDAR"
_STATE_LOCK = threading.RLock()


def _serialized[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with _STATE_LOCK:
            return function(*args, **kwargs)

    return wrapped


def _state() -> tuple[Path, dict[str, Any]]:
    try:
        path = Path(os.environ[CALENDAR_ENVIRONMENT])
    except KeyError as error:
        raise ToolError("The behaviour run has no Calendar state.") from error
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload["calendars"], list) or not isinstance(
            payload["events"], list
        ):
            raise TypeError
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ToolError("The behaviour Calendar state is invalid.") from error
    return path, payload


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _timezone(payload: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(str(payload["timezone"]))
    except (KeyError, ValueError) as error:
        raise ToolError("The behaviour Calendar timezone is invalid.") from error


def _boundary(value: str, timezone: ZoneInfo) -> datetime:
    try:
        if "T" not in value:
            return datetime.combine(date.fromisoformat(value), time(), timezone)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolError("Calendar times must use ISO dates or date-times.") from error
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed


def _event(payload: dict[str, Any], identifier: str) -> dict[str, Any]:
    match = next(
        (event for event in payload["events"] if event["id"] == identifier),
        None,
    )
    if match is None:
        raise ToolError("That Calendar event does not exist in this scenario.")
    return cast(dict[str, Any], match)


def _calendar(payload: dict[str, Any], identifier: str | None) -> dict[str, Any]:
    calendars = payload["calendars"]
    if identifier is None:
        match = next(
            (calendar for calendar in calendars if calendar["is_default"]),
            calendars[0] if calendars else None,
        )
    else:
        match = next(
            (calendar for calendar in calendars if calendar["id"] == identifier),
            None,
        )
    if match is None:
        raise ToolError("That Calendar is not available in this scenario.")
    return cast(dict[str, Any], match)


def _validate_interval(start: str, end: str, timezone: ZoneInfo) -> None:
    if _boundary(end, timezone) <= _boundary(start, timezone):
        raise ToolError("Calendar event end must be later than its start.")


@_serialized
@wraps(real_list_calendars)
def list_calendars() -> dict[str, Any]:
    record_call("list_calendars", {})
    _, payload = _state()
    default = next(
        (calendar["id"] for calendar in payload["calendars"] if calendar["is_default"]),
        None,
    )
    return {
        "calendars": payload["calendars"],
        "default_calendar_id": default,
        "timezone": payload["timezone"],
    }


@_serialized
@wraps(real_search_calendar_events)
def search_calendar_events(
    start: str,
    end: str,
    query: str | None = None,
    calendar_ids: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    arguments = {
        "start": start,
        "end": end,
        "query": query,
        "calendar_ids": calendar_ids,
        "limit": limit,
    }
    record_call("search_calendar_events", arguments)
    _, payload = _state()
    timezone = _timezone(payload)
    selected = set(
        calendar_ids or (calendar["id"] for calendar in payload["calendars"])
    )
    window_start = _boundary(start, timezone)
    window_end = _boundary(end, timezone)
    if window_end <= window_start:
        raise ToolError("Calendar search end must be later than start.")
    terms = tuple(term.casefold() for term in (query or "").split())
    matches = []
    for event in payload["events"]:
        if event["calendar_id"] not in selected:
            continue
        event_start = _boundary(event["start"], timezone)
        event_end = _boundary(event["end"], timezone)
        if event_end <= window_start or event_start >= window_end:
            continue
        searchable = " ".join(
            str(event.get(field) or "")
            for field in ("title", "description", "location")
        ).casefold()
        if terms and not all(term in searchable for term in terms):
            continue
        matches.append(event)
    matches.sort(key=lambda event: _boundary(event["start"], timezone))
    return {
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
        "query": query,
        "events": matches[:limit],
        "total": len(matches),
        "truncated": len(matches) > limit,
        "failed_calendars": [],
    }


@_serialized
@wraps(real_read_calendar_event)
def read_calendar_event(id: str) -> dict[str, Any]:
    record_call("read_calendar_event", {"id": id})
    _, payload = _state()
    return _event(payload, id)


@_serialized
@wraps(real_check_calendar_availability)
def check_calendar_availability(
    start: str, end: str, calendar_ids: list[str] | None = None
) -> dict[str, Any]:
    record_call(
        "check_calendar_availability",
        {"start": start, "end": end, "calendar_ids": calendar_ids},
    )
    _, payload = _state()
    timezone = _timezone(payload)
    selected = set(
        calendar_ids or (calendar["id"] for calendar in payload["calendars"])
    )
    window_start = _boundary(start, timezone)
    window_end = _boundary(end, timezone)
    busy = [
        {
            "start": max(_boundary(event["start"], timezone), window_start).isoformat(),
            "end": min(_boundary(event["end"], timezone), window_end).isoformat(),
            "event_ids": [event["id"]],
            "titles": [event["title"]],
        }
        for event in payload["events"]
        if event["calendar_id"] in selected
        and event["busy"]
        and _boundary(event["end"], timezone) > window_start
        and _boundary(event["start"], timezone) < window_end
    ]
    return {
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
        "busy": busy,
        "failed_calendars": [],
    }


@_serialized
@wraps(real_create_calendar_event)
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
    arguments = {
        "title": title,
        "start": start,
        "end": end,
        "calendar_id": calendar_id,
        "description": description,
        "location": location,
        "attendees": attendees,
        "timezone": timezone,
        "recurrence": recurrence,
        "alarms_minutes_before": alarms_minutes_before,
        "status": status,
        "busy": busy,
    }
    record_call("create_calendar_event", arguments)
    path, payload = _state()
    if not title.strip():
        raise ToolError("A Calendar event needs a title.")
    selected = _calendar(payload, calendar_id)
    selected_timezone = ZoneInfo(timezone) if timezone else _timezone(payload)
    _validate_interval(start, end, selected_timezone)
    occupied = {event["id"] for event in payload["events"]}
    number = len(occupied) + 1
    identifier = f"scenario-event-{number}"
    while identifier in occupied:
        number += 1
        identifier = f"scenario-event-{number}"
    all_day = "T" not in start
    event = {
        "id": identifier,
        "series_id": identifier,
        "calendar_id": selected["id"],
        "calendar": selected["name"],
        "uid": f"{identifier}@ariadne.test",
        "etag": f"scenario-etag-{number}",
        "title": title.strip(),
        "start": start,
        "end": end,
        "all_day": all_day,
        "timezone": None if all_day else str(selected_timezone),
        "description": description.strip()
        if description and description.strip()
        else None,
        "location": location.strip() if location and location.strip() else None,
        "status": status,
        "busy": busy,
        "recurrence": recurrence,
        "recurrence_id": None,
        "is_occurrence": False,
        "organizer": None,
        "attendees": [
            {"email": email, "name": None, "status": None, "role": None, "rsvp": False}
            for email in attendees or ()
        ],
        "alarms": [
            {"minutes_before": minutes, "at": None}
            for minutes in alarms_minutes_before or ()
        ],
    }
    payload["events"].append(event)
    _save(path, payload)
    return event


@_serialized
@wraps(real_update_calendar_event)
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
    arguments = {
        "id": id,
        "scope": scope,
        "expected_etag": expected_etag,
        "title": title,
        "start": start,
        "end": end,
        "description": description,
        "location": location,
        "attendees": attendees,
        "timezone": timezone,
        "recurrence": recurrence,
        "alarms_minutes_before": alarms_minutes_before,
        "status": status,
        "busy": busy,
    }
    record_call("update_calendar_event", arguments)
    path, payload = _state()
    event = _event(payload, id)
    if expected_etag is not None and expected_etag != event["etag"]:
        raise ToolError("The Calendar event changed. Read it again before retrying.")
    if all(
        value is None
        for value in (
            title,
            start,
            end,
            description,
            location,
            attendees,
            timezone,
            recurrence,
            alarms_minutes_before,
            status,
            busy,
        )
    ):
        raise ToolError("Specify at least one Calendar event change.")
    selected_timezone = ZoneInfo(timezone) if timezone else _timezone(payload)
    _validate_interval(start or event["start"], end or event["end"], selected_timezone)
    for field, value in (
        ("title", title.strip() if title is not None else None),
        ("start", start),
        ("end", end),
        ("description", description),
        ("location", location),
        ("recurrence", recurrence),
        ("status", status),
        ("busy", busy),
    ):
        if value is not None:
            event[field] = value or None
    if attendees is not None:
        event["attendees"] = [
            {"email": email, "name": None, "status": None, "role": None, "rsvp": False}
            for email in attendees
        ]
    if alarms_minutes_before is not None:
        event["alarms"] = [
            {"minutes_before": minutes, "at": None} for minutes in alarms_minutes_before
        ]
    if timezone is not None:
        event["timezone"] = timezone
    event["etag"] = f"{event['etag']}-updated"
    _save(path, payload)
    return event


@_serialized
@wraps(real_delete_calendar_event)
def delete_calendar_event(
    id: str,
    scope: UpdateScope = "occurrence",
    expected_etag: str | None = None,
) -> dict[str, Any]:
    record_call(
        "delete_calendar_event",
        {"id": id, "scope": scope, "expected_etag": expected_etag},
    )
    path, payload = _state()
    event = _event(payload, id)
    if expected_etag is not None and expected_etag != event["etag"]:
        raise ToolError("The Calendar event changed. Read it again before retrying.")
    payload["events"].remove(event)
    _save(path, payload)
    return {"status": "deleted", "scope": "series", "id": id}


@_serialized
@wraps(real_respond_to_calendar_invitation)
def respond_to_calendar_invitation(
    id: str,
    response: InvitationResponse,
    expected_etag: str | None = None,
) -> dict[str, Any]:
    record_call(
        "respond_to_calendar_invitation",
        {"id": id, "response": response, "expected_etag": expected_etag},
    )
    _, payload = _state()
    event = _event(payload, id)
    if expected_etag is not None and expected_etag != event["etag"]:
        raise ToolError("The Calendar event changed. Read it again before retrying.")
    return {"status": response, "id": id}


def register_tools(server: FastMCP, annotations: dict[str, bool]) -> None:
    """Register every disposable Calendar operation."""
    server.tool(list_calendars, annotations=annotations)
    server.tool(search_calendar_events, annotations=annotations)
    server.tool(read_calendar_event, annotations=annotations)
    server.tool(check_calendar_availability, annotations=annotations)
    server.tool(create_calendar_event, annotations=annotations)
    server.tool(update_calendar_event, annotations=annotations)
    server.tool(delete_calendar_event, annotations=annotations)
    server.tool(respond_to_calendar_invitation, annotations=annotations)
