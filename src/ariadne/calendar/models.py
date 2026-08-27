"""Opaque references and RFC 5545 normalization for iCloud Calendar."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Alarm, Calendar, Event, vCalAddress, vRecur

CalendarStatus = Literal["confirmed", "tentative", "cancelled"]
UpdateScope = Literal["occurrence", "series"]
InvitationResponse = Literal["accepted", "tentative", "declined"]

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+$")
_PRODID = "-//Ariadne//iCloud Calendar//EN"


class CalendarError(ValueError):
    """A safe, user-facing calendar operation error."""


class CalendarConflict(CalendarError):
    """A calendar resource changed between reading and writing it."""


@dataclass(frozen=True, slots=True)
class EventReference:
    calendar_url: str
    uid: str
    recurrence_id: str | None = None
    resource_url: str | None = None


def _token(prefix: str, payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return prefix + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _payload(value: str, prefix: str) -> object:
    try:
        if not value.startswith(prefix):
            raise ValueError
        token = value.removeprefix(prefix)
        return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as error:
        raise CalendarError(
            "That calendar id is not valid. List calendars again."
        ) from error


def encode_calendar_id(url: str) -> str:
    return _token("calendar:", [1, url])


def decode_calendar_id(value: str) -> str:
    payload = _payload(value, "calendar:")
    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or payload[0] != 1
        or not isinstance(payload[1], str)
        or not payload[1]
    ):
        raise CalendarError("That calendar id is not valid. List calendars again.")
    return payload[1]


def encode_event_id(reference: EventReference) -> str:
    if reference.resource_url is None:
        # Version 1 ids remain useful for events created before Ariadne began
        # retaining the CalDAV resource href. Their href can usually be
        # reconstructed from the UID.
        return _token(
            "calendar-event:",
            [1, reference.calendar_url, reference.uid, reference.recurrence_id],
        )
    return _token(
        "calendar-event:",
        [
            2,
            reference.calendar_url,
            reference.uid,
            reference.recurrence_id,
            reference.resource_url,
        ],
    )


def decode_event_id(value: str) -> EventReference:
    payload = _payload(value, "calendar-event:")
    if isinstance(payload, list):
        if (
            len(payload) == 4
            and payload[0] == 1
            and isinstance(payload[1], str)
            and payload[1]
            and isinstance(payload[2], str)
            and payload[2]
            and (payload[3] is None or isinstance(payload[3], str))
        ):
            return EventReference(payload[1], payload[2], payload[3])
        if (
            len(payload) == 5
            and payload[0] == 2
            and isinstance(payload[1], str)
            and payload[1]
            and isinstance(payload[2], str)
            and payload[2]
            and (payload[3] is None or isinstance(payload[3], str))
            and isinstance(payload[4], str)
            and payload[4]
        ):
            return EventReference(payload[1], payload[2], payload[3], payload[4])
    raise CalendarError(
        "That calendar event id is not valid. Search the calendar again."
    )


def configured_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise CalendarError("The configured calendar timezone is not valid.") from error


def _date_only(value: str) -> bool:
    return _DATE.fullmatch(value.strip()) is not None


def parse_boundary(value: str, timezone: ZoneInfo, name: str) -> datetime:
    """Parse a bounded-search timestamp, treating dates as local midnight."""
    value = value.strip()
    try:
        if _date_only(value):
            return datetime.combine(date.fromisoformat(value), time(), timezone)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CalendarError(f"{name} must be an ISO date or ISO date-time.") from error
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed


def parse_event_times(
    start: str, end: str, timezone: ZoneInfo
) -> tuple[date | datetime, date | datetime]:
    """Parse an event interval while preserving all-day date values."""
    start, end = start.strip(), end.strip()
    if _date_only(start) != _date_only(end):
        raise CalendarError("Event start and end must both be dates or date-times.")
    try:
        if _date_only(start):
            parsed_start: date | datetime = date.fromisoformat(start)
            parsed_end: date | datetime = date.fromisoformat(end)
        else:
            parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if parsed_start.tzinfo is None:
                parsed_start = parsed_start.replace(tzinfo=timezone)
            else:
                parsed_start = parsed_start.astimezone(timezone)
            if parsed_end.tzinfo is None:
                parsed_end = parsed_end.replace(tzinfo=timezone)
            else:
                parsed_end = parsed_end.astimezone(timezone)
    except ValueError as error:
        raise CalendarError("Event times must use ISO dates or date-times.") from error
    if parsed_end <= parsed_start:
        raise CalendarError("Event end must be later than its start.")
    return parsed_start, parsed_end


def _validate_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise CalendarError("A calendar event needs a title.")
    return value


def normalize_attendees(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    attendees = tuple(dict.fromkeys(value.strip().casefold() for value in values))
    if any(not _EMAIL.fullmatch(value) for value in attendees):
        raise CalendarError("Every calendar attendee must be an email address.")
    return attendees


def parse_recurrence(value: str) -> vRecur:
    raw = value.strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[6:]
    if not raw or "\n" in raw or "\r" in raw:
        raise CalendarError("Recurrence must be one RFC 5545 RRULE value.")
    try:
        recurrence = cast(vRecur, vRecur.from_ical(raw))
    except (TypeError, ValueError) as error:
        raise CalendarError("Recurrence is not a valid RFC 5545 RRULE.") from error
    if "FREQ" not in recurrence:
        raise CalendarError("Recurrence must include FREQ.")
    return recurrence


def normalize_alarms(values: list[int] | tuple[int, ...]) -> tuple[int, ...]:
    alarms = tuple(sorted(set(values), reverse=True))
    if any(value < 0 or value > 525_600 for value in alarms):
        raise CalendarError(
            "Alarm offsets must be between 0 and 525600 minutes before the event."
        )
    return alarms


def _add_alarms(component: Event, values: tuple[int, ...], title: str) -> None:
    for minutes in values:
        alarm = Alarm()
        alarm.add("ACTION", "DISPLAY")
        alarm.add("DESCRIPTION", title)
        alarm.add("TRIGGER", timedelta(minutes=-minutes))
        component.add_component(alarm)


def _add_attendees(component: Event, attendees: tuple[str, ...]) -> None:
    for email in attendees:
        attendee = vCalAddress(f"mailto:{email}")
        attendee.params["ROLE"] = "REQ-PARTICIPANT"
        attendee.params["PARTSTAT"] = "NEEDS-ACTION"
        attendee.params["RSVP"] = "TRUE"
        component.add("ATTENDEE", attendee, encode=False)


def new_event(
    *,
    title: str,
    start: str,
    end: str,
    timezone: ZoneInfo,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    recurrence: str | None = None,
    alarms_minutes_before: list[int] | None = None,
    status: CalendarStatus = "confirmed",
    busy: bool = True,
) -> tuple[str, tuple[str, ...]]:
    """Build one complete VCALENDAR and return scheduling attendees separately."""
    parsed_start, parsed_end = parse_event_times(start, end, timezone)
    title = _validate_title(title)
    normalized_attendees = normalize_attendees(attendees or [])
    calendar = Calendar()
    calendar.add("PRODID", _PRODID)
    calendar.add("VERSION", "2.0")
    calendar.add("CALSCALE", "GREGORIAN")
    event = Event()
    event.add("UID", f"{uuid.uuid4()}@ariadne")
    event.add("DTSTAMP", datetime.now(UTC))
    event.add("CREATED", datetime.now(UTC))
    event.add("SUMMARY", title)
    event.add("DTSTART", parsed_start)
    event.add("DTEND", parsed_end)
    event.add("STATUS", status.upper())
    event.add("TRANSP", "OPAQUE" if busy else "TRANSPARENT")
    if description is not None and description.strip():
        event.add("DESCRIPTION", description.strip())
    if location is not None and location.strip():
        event.add("LOCATION", location.strip())
    if recurrence is not None:
        event.add("RRULE", parse_recurrence(recurrence))
    _add_alarms(event, normalize_alarms(alarms_minutes_before or []), title)
    calendar.add_component(event)
    return calendar.to_ical().decode(), normalized_attendees


def _decoded(component: Any, name: str) -> Any | None:
    try:
        return component.decoded(name)
    except (KeyError, ValueError):
        return None


def _temporal(value: date | datetime) -> str:
    return value.isoformat()


def recurrence_id(component: Event) -> str | None:
    value = _decoded(component, "RECURRENCE-ID")
    return _temporal(value) if isinstance(value, (date, datetime)) else None


def event_interval(component: Event) -> tuple[date | datetime, date | datetime]:
    start = _decoded(component, "DTSTART")
    if not isinstance(start, (date, datetime)):
        raise CalendarError("The calendar event has no valid start time.")
    end = _decoded(component, "DTEND")
    if not isinstance(end, (date, datetime)):
        duration = _decoded(component, "DURATION")
        if isinstance(duration, timedelta):
            end = start + duration
        elif isinstance(start, datetime):
            end = start
        else:
            end = start + timedelta(days=1)
    return start, end


def _property_text(component: Event, name: str) -> str | None:
    value = component.get(name)
    text = str(value).strip() if value is not None else ""
    return text or None


def _timezone_name(component: Event) -> str | None:
    start_property = component.get("DTSTART")
    if start_property is None:
        return None
    tzid = start_property.params.get("TZID")
    if tzid:
        return str(tzid)
    value = _decoded(component, "DTSTART")
    tzinfo = value.tzinfo if isinstance(value, datetime) else None
    return getattr(tzinfo, "key", str(tzinfo) if tzinfo else None)


def event_timezone(component: Event, fallback: ZoneInfo) -> ZoneInfo:
    value = _decoded(component, "DTSTART")
    if not isinstance(value, datetime) or value.tzinfo is None:
        return fallback
    key = getattr(value.tzinfo, "key", None)
    if isinstance(key, str):
        return configured_timezone(key)
    if value.tzinfo is UTC or str(value.tzinfo).upper() == "UTC":
        return ZoneInfo("UTC")
    return fallback


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _attendee_payload(component: Event) -> list[dict[str, Any]]:
    result = []
    for attendee in _values(component.get("ATTENDEE")):
        address = str(attendee)
        if address.casefold().startswith("mailto:"):
            address = address[7:]
        params = attendee.params
        result.append(
            {
                "email": address,
                "name": str(params.get("CN", "")) or None,
                "status": str(params.get("PARTSTAT", "")) or None,
                "role": str(params.get("ROLE", "")) or None,
                "rsvp": str(params.get("RSVP", "")).upper() == "TRUE",
            }
        )
    return result


def _organizer_payload(component: Event) -> dict[str, str | None] | None:
    organizer = component.get("ORGANIZER")
    if organizer is None:
        return None
    address = str(organizer)
    if address.casefold().startswith("mailto:"):
        address = address[7:]
    return {"email": address, "name": str(organizer.params.get("CN", "")) or None}


def _recurrence_payload(component: Event) -> str | None:
    recurrence = component.get("RRULE")
    if recurrence is None:
        return None
    encoded = (
        recurrence.to_ical().decode()
        if hasattr(recurrence, "to_ical")
        else str(recurrence)
    )
    return "RRULE:" + encoded


def _alarm_payload(component: Event) -> list[dict[str, Any]]:
    alarms: list[dict[str, Any]] = []
    for alarm in component.subcomponents:
        if not isinstance(alarm, Alarm):
            continue
        trigger = _decoded(alarm, "TRIGGER")
        if isinstance(trigger, timedelta) and trigger.total_seconds() <= 0:
            alarms.append(
                {"minutes_before": int(-trigger.total_seconds() // 60), "at": None}
            )
        elif isinstance(trigger, datetime):
            alarms.append({"minutes_before": None, "at": trigger.isoformat()})
    return alarms


def event_payload(
    *,
    calendar_url: str,
    calendar_name: str,
    resource: Any,
    component: Event | None = None,
) -> dict[str, Any]:
    component = component or resource.get_icalendar_component()
    uid = _property_text(component, "UID") or str(resource.id or "")
    if not uid:
        raise CalendarError("The calendar server returned an event without a UID.")
    occurrence = recurrence_id(component)
    raw_resource_url = getattr(resource, "url", None)
    resource_url = str(raw_resource_url) if raw_resource_url is not None else None
    reference = EventReference(calendar_url, uid, occurrence, resource_url)
    start, end = event_interval(component)
    all_day = not isinstance(start, datetime)
    status = (_property_text(component, "STATUS") or "CONFIRMED").casefold()
    transparency = (_property_text(component, "TRANSP") or "OPAQUE").upper()
    return {
        "id": encode_event_id(reference),
        "series_id": encode_event_id(
            EventReference(calendar_url, uid, resource_url=resource_url)
        ),
        "calendar_id": encode_calendar_id(calendar_url),
        "calendar": calendar_name,
        "uid": uid,
        "etag": getattr(resource, "etag", None),
        "title": _property_text(component, "SUMMARY") or "(untitled)",
        "start": _temporal(start),
        "end": _temporal(end),
        "all_day": all_day,
        "timezone": None if all_day else _timezone_name(component),
        "description": _property_text(component, "DESCRIPTION"),
        "location": _property_text(component, "LOCATION"),
        "status": status,
        "busy": transparency != "TRANSPARENT" and status != "cancelled",
        "recurrence": _recurrence_payload(component),
        "recurrence_id": occurrence,
        "is_occurrence": occurrence is not None,
        "organizer": _organizer_payload(component),
        "attendees": _attendee_payload(component),
        "alarms": _alarm_payload(component),
    }


def event_matches(payload: dict[str, Any], query: str | None) -> bool:
    if query is None or not query.strip():
        return True
    terms = tuple(term.casefold() for term in query.split() if term.strip())
    attendees = " ".join(
        f"{item.get('name') or ''} {item.get('email') or ''}"
        for item in payload["attendees"]
    )
    haystack = " ".join(
        str(payload.get(field) or "") for field in ("title", "description", "location")
    )
    haystack = f"{haystack} {attendees}".casefold()
    return all(term in haystack for term in terms)


def event_sort_key(payload: dict[str, Any], timezone: ZoneInfo) -> datetime:
    value = str(payload["start"])
    if payload["all_day"]:
        return datetime.combine(date.fromisoformat(value), time(), timezone)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed


def replace_text(component: Event, name: str, value: str) -> None:
    component.pop(name, None)
    value = value.strip()
    if value:
        component.add(name, value)


def replace_title(component: Event, value: str) -> None:
    component.pop("SUMMARY", None)
    component.add("SUMMARY", _validate_title(value))


def _replacement_time(
    value: str, current: date | datetime, timezone: ZoneInfo
) -> date | datetime:
    value = value.strip()
    if isinstance(current, datetime):
        if _date_only(value):
            raise CalendarError("A timed event needs a date-time value.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise CalendarError("Event times must use ISO date-times.") from error
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone)
        return parsed.astimezone(timezone)
    if not _date_only(value):
        raise CalendarError("An all-day event needs a date-only value.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CalendarError("Event times must use ISO dates.") from error


def replace_times(
    component: Event,
    *,
    start: str | None,
    end: str | None,
    timezone: ZoneInfo,
) -> None:
    current_start, current_end = event_interval(component)
    if start is not None and end is not None:
        new_start, new_end = parse_event_times(start, end, timezone)
    elif start is not None:
        new_start = _replacement_time(start, current_start, timezone)
        if isinstance(new_start, datetime):
            if not isinstance(current_start, datetime) or not isinstance(
                current_end, datetime
            ):
                raise CalendarError("The existing event has inconsistent times.")
            new_end = new_start + (current_end - current_start)
        else:
            if isinstance(current_start, datetime) or isinstance(current_end, datetime):
                raise CalendarError("The existing event has inconsistent times.")
            new_end = new_start + (current_end - current_start)
    elif end is not None:
        new_start = current_start
        new_end = _replacement_time(end, current_end, timezone)
    else:
        return
    if isinstance(new_start, datetime) != isinstance(new_end, datetime):
        raise CalendarError("Event start and end must both be dates or date-times.")
    if new_end <= new_start:
        raise CalendarError("Event end must be later than its start.")
    component.pop("DTSTART", None)
    component.pop("DTEND", None)
    component.pop("DURATION", None)
    component.add("DTSTART", new_start)
    component.add("DTEND", new_end)


def replace_recurrence(component: Event, value: str) -> None:
    component.pop("RRULE", None)
    if value.strip():
        component.add("RRULE", parse_recurrence(value))


def replace_alarms(component: Event, values: list[int]) -> None:
    component.subcomponents = [
        item for item in component.subcomponents if not isinstance(item, Alarm)
    ]
    _add_alarms(
        component,
        normalize_alarms(values),
        _property_text(component, "SUMMARY") or "Calendar event",
    )


def replace_attendees(component: Event, values: list[str]) -> None:
    component.pop("ATTENDEE", None)
    _add_attendees(component, normalize_attendees(values))


def replace_status(component: Event, value: CalendarStatus) -> None:
    component.pop("STATUS", None)
    component.add("STATUS", value.upper())


def replace_busy(component: Event, value: bool) -> None:
    component.pop("TRANSP", None)
    component.add("TRANSP", "OPAQUE" if value else "TRANSPARENT")


def occurrence_window(value: str, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    start = parse_boundary(value, timezone, "recurrence id")
    return start - timedelta(days=2), start + timedelta(days=2)


def exclude_occurrence(master: Any, occurrence: Event) -> None:
    """Add an EXDATE and remove a matching override from a recurring resource."""
    recurrence_property = occurrence.get("RECURRENCE-ID")
    if recurrence_property is None:
        raise CalendarError("That event is not a recurrence occurrence.")
    recurrence_value = _decoded(occurrence, "RECURRENCE-ID")
    if not isinstance(recurrence_value, (date, datetime)):
        raise CalendarError("That occurrence has no valid recurrence id.")
    target = _temporal(recurrence_value)
    with master.edit_icalendar_instance() as calendar:
        components = [
            item for item in calendar.subcomponents if isinstance(item, Event)
        ]
        roots = [item for item in components if item.get("RECURRENCE-ID") is None]
        if len(roots) != 1:
            raise CalendarError("The recurring event has no unambiguous series root.")
        root = roots[0]
        retained = []
        for item in calendar.subcomponents:
            if isinstance(item, Event) and recurrence_id(item) == target:
                continue
            retained.append(item)
        calendar.subcomponents = retained
        parameters = {
            str(key): str(value)
            for key, value in recurrence_property.params.items()
            if str(key).upper() != "RANGE"
        }
        if parameters:
            root.add("EXDATE", recurrence_value, parameters=parameters)
        else:
            root.add("EXDATE", recurrence_value)


def replace_occurrence(master: Any, occurrence: Event) -> Event:
    """Store one recurrence override in an already-loaded series resource."""
    target = recurrence_id(occurrence)
    if target is None:
        raise CalendarError("That event is not a recurrence occurrence.")
    replacement = copy.deepcopy(occurrence)
    with master.edit_icalendar_instance() as calendar:
        calendar.subcomponents = [
            item
            for item in calendar.subcomponents
            if not (isinstance(item, Event) and recurrence_id(item) == target)
        ]
        calendar.add_component(replacement)
    return replacement
