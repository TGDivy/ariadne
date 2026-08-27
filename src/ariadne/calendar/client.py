"""On-demand CalDAV access for the authenticated iCloud account."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, cast

from caldav import DAVClient
from caldav.lib.error import AuthorizationError, NotFoundError, RateLimitError
from icalendar import Event

from .models import (
    CalendarConflict,
    CalendarError,
    CalendarStatus,
    EventReference,
    InvitationResponse,
    UpdateScope,
    configured_timezone,
    decode_calendar_id,
    decode_event_id,
    encode_calendar_id,
    event_matches,
    event_payload,
    event_sort_key,
    event_timezone,
    exclude_occurrence,
    new_event,
    occurrence_window,
    recurrence_id,
    replace_alarms,
    replace_attendees,
    replace_busy,
    replace_recurrence,
    replace_status,
    replace_text,
    replace_times,
    replace_title,
)

LOGGER = logging.getLogger(__name__)
ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"


@dataclass(frozen=True, slots=True)
class _CalendarHandle:
    url: str
    name: str
    supports_events: bool
    calendar: Any


def _calendar_url(calendar: Any) -> str:
    return str(calendar.url).rstrip("/") + "/"


def _component(resource: Any) -> Event:
    component = resource.get_icalendar_component()
    if not isinstance(component, Event):
        raise CalendarError("The calendar server returned a non-event resource.")
    return component


class ICloudCalendar:
    """Discover calendars and perform bounded, on-demand event operations."""

    def __init__(
        self,
        username: str,
        app_password: str,
        *,
        timezone: str = "UTC",
        default_calendar: str | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.timezone = configured_timezone(timezone)
        self.default_calendar = default_calendar
        factory = client_factory or cast(Callable[..., Any], DAVClient)
        self._client = factory(
            url=ICLOUD_CALDAV_URL,
            username=username,
            password=app_password,
            timeout=30,
            ssl_verify_cert=True,
            rate_limit_handle=True,
        )
        self._handles_cache: tuple[_CalendarHandle, ...] | None = None

    def __enter__(self) -> ICloudCalendar:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _handles(self) -> tuple[_CalendarHandle, ...]:
        if self._handles_cache is not None:
            return self._handles_cache
        principal = self._client.get_principal()
        handles = []
        for calendar in principal.get_calendars():
            try:
                name = calendar.get_display_name() or calendar.name
            except (AuthorizationError, RateLimitError):
                raise
            except Exception:
                name = calendar.name
            supports_events = True
            try:
                components = {
                    str(value).upper() for value in calendar.get_supported_components()
                }
                supports_events = "VEVENT" in components
            except Exception:
                LOGGER.debug(
                    "Could not inspect calendar component support for %s",
                    name,
                    exc_info=True,
                )
            handles.append(
                _CalendarHandle(
                    url=_calendar_url(calendar),
                    name=str(name or "Unnamed calendar"),
                    supports_events=supports_events,
                    calendar=calendar,
                )
            )
        self._handles_cache = tuple(
            sorted(handles, key=lambda item: item.name.casefold())
        )
        return self._handles_cache

    def _event_handles(self) -> tuple[_CalendarHandle, ...]:
        handles = tuple(handle for handle in self._handles() if handle.supports_events)
        if not handles:
            raise CalendarError("The iCloud account has no event calendars.")
        return handles

    def _handle_by_url(self, url: str) -> _CalendarHandle:
        handle = next((item for item in self._event_handles() if item.url == url), None)
        if handle is None:
            raise CalendarError(
                "That calendar is no longer available. List calendars again."
            )
        return handle

    def _selected_handles(
        self, calendar_ids: list[str] | None
    ) -> tuple[_CalendarHandle, ...]:
        if calendar_ids is None:
            return self._event_handles()
        if not calendar_ids:
            raise CalendarError("Select at least one calendar.")
        urls = tuple(dict.fromkeys(decode_calendar_id(value) for value in calendar_ids))
        return tuple(self._handle_by_url(url) for url in urls)

    def _default_handle(self, calendar_id: str | None = None) -> _CalendarHandle:
        if calendar_id is not None:
            return self._handle_by_url(decode_calendar_id(calendar_id))
        handles = self._event_handles()
        if self.default_calendar is not None:
            matches = tuple(
                handle
                for handle in handles
                if handle.name.casefold() == self.default_calendar.casefold()
            )
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise CalendarError(
                    "The configured default calendar "
                    f'"{self.default_calendar}" was not found.'
                )
            raise CalendarError(
                "The configured default calendar name is ambiguous; use a calendar id."
            )
        if len(handles) == 1:
            return handles[0]
        raise CalendarError(
            "Choose a calendar id or configure calendar.default_calendar."
        )

    def list_calendars(self) -> dict[str, Any]:
        handles = self._handles()
        default: _CalendarHandle | None = None
        try:
            default = self._default_handle()
        except CalendarError:
            pass
        return {
            "calendars": [
                {
                    "id": encode_calendar_id(handle.url),
                    "name": handle.name,
                    "supports_events": handle.supports_events,
                    "is_default": default is not None and handle.url == default.url,
                }
                for handle in handles
            ],
            "default_calendar_id": (
                encode_calendar_id(default.url) if default is not None else None
            ),
            "timezone": str(self.timezone),
        }

    def _search_handles(
        self,
        *,
        start: datetime,
        end: datetime,
        handles: tuple[_CalendarHandle, ...],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        results: list[dict[str, Any]] = []
        failed = []
        last_error: Exception | None = None
        for handle in handles:
            try:
                calendar_results = []
                resources = handle.calendar.search(
                    event=True,
                    start=start,
                    end=end,
                    expand=True,
                    split_expanded=True,
                )
                for resource in resources:
                    calendar_results.append(
                        event_payload(
                            calendar_url=handle.url,
                            calendar_name=handle.name,
                            resource=resource,
                            component=_component(resource),
                        )
                    )
                results.extend(calendar_results)
            except Exception as error:
                failed.append(handle.name)
                last_error = error
                LOGGER.warning(
                    "Could not search iCloud calendar %s (%s)",
                    handle.name,
                    type(error).__name__,
                )
        if len(failed) == len(handles):
            failure = CalendarError("iCloud could not search any selected calendar.")
            if last_error is not None:
                raise failure from last_error
            raise failure
        return results, failed

    def search_events(
        self,
        start: str,
        end: str,
        *,
        query: str | None = None,
        calendar_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        from .models import parse_boundary

        if not 1 <= limit <= 200:
            raise CalendarError("Calendar search limit must be between 1 and 200.")
        parsed_start = parse_boundary(start, self.timezone, "start")
        parsed_end = parse_boundary(end, self.timezone, "end")
        if parsed_end <= parsed_start:
            raise CalendarError("Calendar search end must be later than start.")
        selected = self._selected_handles(calendar_ids)
        events, failed = self._search_handles(
            start=parsed_start, end=parsed_end, handles=selected
        )
        matched = [event for event in events if event_matches(event, query)]
        matched.sort(key=lambda event: event_sort_key(event, self.timezone))
        return {
            "start": parsed_start.isoformat(),
            "end": parsed_end.isoformat(),
            "query": query,
            "events": matched[:limit],
            "total": len(matched),
            "truncated": len(matched) > limit,
            "failed_calendars": failed,
        }

    def _series(self, handle: _CalendarHandle, uid: str) -> Any:
        try:
            resource = handle.calendar.get_event_by_uid(uid)
            resource.load()
            return resource
        except NotFoundError as error:
            raise CalendarError(
                "That calendar event is no longer available. Search again."
            ) from error

    def _resolve(
        self, reference: EventReference, *, scope: UpdateScope = "occurrence"
    ) -> tuple[_CalendarHandle, Any]:
        handle = self._handle_by_url(reference.calendar_url)
        if scope == "series" or reference.recurrence_id is None:
            return handle, self._series(handle, reference.uid)
        start, end = occurrence_window(reference.recurrence_id, self.timezone)
        resources = handle.calendar.search(
            event=True,
            uid=reference.uid,
            start=start,
            end=end,
            expand=True,
            split_expanded=True,
        )
        resource = next(
            (
                item
                for item in resources
                if recurrence_id(_component(item)) == reference.recurrence_id
            ),
            None,
        )
        if resource is None:
            raise CalendarError(
                "That recurrence occurrence is no longer available. Search again."
            )
        return handle, resource

    def _check_etag(
        self,
        handle: _CalendarHandle,
        resource: Any,
        reference: EventReference,
        expected_etag: str | None,
    ) -> None:
        if expected_etag is None:
            return
        actual = getattr(resource, "etag", None)
        if actual is None and reference.recurrence_id is not None:
            actual = getattr(self._series(handle, reference.uid), "etag", None)
        if actual != expected_etag:
            raise CalendarConflict(
                "The calendar event changed since it was read. "
                "Read it again before writing."
            )

    def read_event(self, value: str) -> dict[str, Any]:
        reference = decode_event_id(value)
        handle, resource = self._resolve(reference)
        return event_payload(
            calendar_url=handle.url,
            calendar_name=handle.name,
            resource=resource,
            component=_component(resource),
        )

    def create_event(
        self,
        *,
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
        handle = self._default_handle(calendar_id)
        selected_timezone = (
            configured_timezone(timezone) if timezone is not None else self.timezone
        )
        ical, normalized_attendees = new_event(
            title=title,
            start=start,
            end=end,
            timezone=selected_timezone,
            description=description,
            location=location,
            attendees=attendees,
            recurrence=recurrence,
            alarms_minutes_before=alarms_minutes_before,
            status=status,
            busy=busy,
        )
        if normalized_attendees:
            resource = handle.calendar.save_with_invites(ical, normalized_attendees)
        else:
            resource = handle.calendar.add_event(ical=ical, no_overwrite=True)
        return event_payload(
            calendar_url=handle.url,
            calendar_name=handle.name,
            resource=resource,
            component=_component(resource),
        )

    def update_event(
        self,
        value: str,
        *,
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
        changes = (
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
        if all(change is None for change in changes):
            raise CalendarError("Specify at least one calendar event change.")
        if timezone is not None and start is None and end is None:
            raise CalendarError("An event timezone change also needs a start or end.")
        reference = decode_event_id(value)
        if recurrence is not None and scope != "series":
            raise CalendarError("Recurrence rules can only be changed for a series.")
        handle, resource = self._resolve(reference, scope=scope)
        self._check_etag(handle, resource, reference, expected_etag)
        selected_timezone = (
            configured_timezone(timezone)
            if timezone is not None
            else event_timezone(_component(resource), self.timezone)
        )
        with resource.edit_icalendar_component() as component:
            if title is not None:
                replace_title(component, title)
            if start is not None or end is not None:
                replace_times(
                    component,
                    start=start,
                    end=end,
                    timezone=selected_timezone,
                )
            if description is not None:
                replace_text(component, "DESCRIPTION", description)
            if location is not None:
                replace_text(component, "LOCATION", location)
            if attendees is not None:
                replace_attendees(component, attendees)
            if recurrence is not None:
                replace_recurrence(component, recurrence)
            if alarms_minutes_before is not None:
                replace_alarms(component, alarms_minutes_before)
            if status is not None:
                replace_status(component, status)
            if busy is not None:
                replace_busy(component, busy)
        if attendees and _component(resource).get("ORGANIZER") is None:
            resource.add_organizer()
        resource.save()
        result_reference = EventReference(
            reference.calendar_url,
            reference.uid,
            reference.recurrence_id if scope == "occurrence" else None,
        )
        result_handle, result = self._resolve(result_reference, scope=scope)
        return event_payload(
            calendar_url=result_handle.url,
            calendar_name=result_handle.name,
            resource=result,
            component=_component(result),
        )

    def delete_event(
        self,
        value: str,
        *,
        scope: UpdateScope = "occurrence",
        expected_etag: str | None = None,
    ) -> dict[str, str]:
        reference = decode_event_id(value)
        handle, resource = self._resolve(reference, scope=scope)
        self._check_etag(handle, resource, reference, expected_etag)
        if scope == "occurrence" and reference.recurrence_id is not None:
            master = self._series(handle, reference.uid)
            self._check_etag(handle, master, reference, expected_etag)
            exclude_occurrence(master, _component(resource))
            master.save()
        else:
            resource.delete()
        return {
            "status": "deleted",
            "scope": (
                "occurrence"
                if scope == "occurrence" and reference.recurrence_id is not None
                else "series"
            ),
            "id": value,
        }

    def respond_to_invitation(
        self,
        value: str,
        response: InvitationResponse,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, str]:
        reference = decode_event_id(value)
        handle, resource = self._resolve(reference, scope="series")
        self._check_etag(handle, resource, reference, expected_etag)
        methods = {
            "accepted": "accept_invite",
            "tentative": "tentatively_accept_invite",
            "declined": "decline_invite",
        }
        if resource.is_invite_request():
            getattr(resource, methods[response])(handle.calendar)
        else:
            try:
                resource.change_attendee_status(partstat=response.upper())
            except NotFoundError as error:
                raise CalendarError(
                    "That event is not an invitation for the configured iCloud account."
                ) from error
            resource.save()
        return {"status": response, "id": value}

    def free_busy(
        self,
        start: str,
        end: str,
        *,
        calendar_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        from .models import parse_boundary

        parsed_start = parse_boundary(start, self.timezone, "start")
        parsed_end = parse_boundary(end, self.timezone, "end")
        if parsed_end <= parsed_start:
            raise CalendarError("Free/busy end must be later than start.")
        events, failed = self._search_handles(
            start=parsed_start,
            end=parsed_end,
            handles=self._selected_handles(calendar_ids),
        )
        intervals = []
        for event in events:
            if not event["busy"]:
                continue
            event_start = self._payload_time(event["start"], event["all_day"])
            event_end = self._payload_time(event["end"], event["all_day"])
            if event_end <= parsed_start or event_start >= parsed_end:
                continue
            intervals.append(
                {
                    "start": max(event_start, parsed_start),
                    "end": min(event_end, parsed_end),
                    "event_ids": [event["id"]],
                    "titles": [event["title"]],
                }
            )
        intervals.sort(key=lambda item: cast(datetime, item["start"]))
        merged: list[dict[str, Any]] = []
        for interval in intervals:
            if merged and interval["start"] <= merged[-1]["end"]:
                merged[-1]["end"] = max(merged[-1]["end"], interval["end"])
                merged[-1]["event_ids"].extend(interval["event_ids"])
                merged[-1]["titles"].extend(interval["titles"])
            else:
                merged.append(interval)
        return {
            "start": parsed_start.isoformat(),
            "end": parsed_end.isoformat(),
            "busy": [
                {
                    **interval,
                    "start": interval["start"].isoformat(),
                    "end": interval["end"].isoformat(),
                }
                for interval in merged
            ],
            "failed_calendars": failed,
        }

    def _payload_time(self, value: str, all_day: bool) -> datetime:
        if all_day:
            return datetime.combine(date.fromisoformat(value), time(), self.timezone)
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=self.timezone) if parsed.tzinfo is None else parsed
