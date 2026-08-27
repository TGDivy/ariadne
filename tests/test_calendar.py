from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, time

import pytest
from caldav import Event as DAVEvent
from caldav.lib.error import ConsistencyError, NotFoundError, PutError, ReportError
from icalendar import Calendar, Event, vCalAddress

from ariadne.calendar import CalendarConflict, CalendarError, ICloudCalendar
from ariadne.calendar.models import (
    EventReference,
    decode_event_id,
    encode_event_id,
    event_interval,
    recurrence_id,
)


def _calendar_with(component: Event) -> str:
    calendar = Calendar()
    calendar.add("PRODID", "-//Ariadne tests//EN")
    calendar.add("VERSION", "2.0")
    calendar.add_component(component)
    return calendar.to_ical().decode()


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return datetime.combine(value, time(), UTC)


class FakeEvent:
    def __init__(
        self,
        calendar: FakeCalendar,
        data: str,
        *,
        etag: str = '"1"',
        fragment: bool = False,
        url: str | None = None,
    ) -> None:
        self.calendar = calendar
        self.inner = DAVEvent(data=data)
        self.etag = etag
        self.fragment = fragment
        self.url = url

    @property
    def id(self) -> str | None:
        return self.inner.id

    @property
    def data(self) -> str:
        return self.inner.data

    def get_icalendar_component(self) -> Event:
        return self.inner.get_icalendar_component()

    def edit_icalendar_component(self) -> AbstractContextManager[Event]:
        return self.inner.edit_icalendar_component()

    def edit_icalendar_instance(self) -> AbstractContextManager[Calendar]:
        return self.inner.edit_icalendar_instance()

    def load(self) -> FakeEvent:
        return self

    def save(self) -> FakeEvent:
        if self.id is None:
            raise AssertionError("test events need UIDs")
        if self.fragment:
            master = self.calendar.events_by_uid[self.id]
            fragment = self.get_icalendar_component()
            with master.edit_icalendar_instance() as calendar:
                existing = [
                    index
                    for index, component in enumerate(calendar.subcomponents)
                    if isinstance(component, Event)
                    and recurrence_id(component) == recurrence_id(fragment)
                ]
                if existing:
                    calendar.subcomponents[existing[0]] = fragment
                else:
                    calendar.add_component(fragment)
            master._advance_etag()
            return master
        self._advance_etag()
        return self

    def _advance_etag(self) -> None:
        self.etag = f'"{int(self.etag.strip(chr(34))) + 1}"'

    def delete(self) -> None:
        if self.id is not None:
            self.calendar.events_by_uid.pop(self.id, None)

    def add_organizer(self) -> None:
        with self.edit_icalendar_component() as component:
            if component.get("ORGANIZER") is None:
                component.add("ORGANIZER", "mailto:person@example.com")

    def is_invite_request(self) -> bool:
        return False

    def change_attendee_status(self, *, partstat: str) -> None:
        with self.edit_icalendar_component() as component:
            attendees = component.get("ATTENDEE")
            attendees = attendees if isinstance(attendees, list) else [attendees]
            if not attendees or attendees == [None]:
                raise NotFoundError("not invited")
            attendees[0].params["PARTSTAT"] = partstat


class FakeCalendar:
    def __init__(self, name: str, suffix: str, supports_events: bool = True) -> None:
        self.name = name
        self.url = f"https://caldav.icloud.test/calendars/{suffix}/"
        self.supports_events = supports_events
        self.events_by_uid: dict[str, FakeEvent] = {}
        self.uid_queries = 0

    def get_display_name(self) -> str:
        return self.name

    def get_supported_components(self) -> list[str]:
        return ["VEVENT"] if self.supports_events else ["VTODO"]

    def add_event(self, *, ical: str, no_overwrite: bool = False) -> FakeEvent:
        resource = FakeEvent(self, ical)
        assert resource.id is not None
        if no_overwrite and resource.id in self.events_by_uid:
            raise ConsistencyError("duplicate")
        resource.url = f"{self.url}{resource.id.replace('@', '%40')}.ics"
        self.events_by_uid[resource.id] = resource
        return resource

    def save_with_invites(self, ical: str, attendees: tuple[str, ...]) -> FakeEvent:
        parsed = Calendar.from_ical(ical)
        component = parsed.walk("VEVENT")[0]
        component.add("ORGANIZER", "mailto:person@example.com")
        for email in attendees:
            attendee = vCalAddress(f"mailto:{email}")
            attendee.params["PARTSTAT"] = "NEEDS-ACTION"
            component.add("ATTENDEE", attendee, encode=False)
        return self.add_event(ical=parsed.to_ical().decode(), no_overwrite=True)

    def get_event_by_uid(self, uid: str) -> FakeEvent:
        self.uid_queries += 1
        try:
            return self.events_by_uid[uid]
        except KeyError as error:
            raise NotFoundError(uid) from error

    def event_by_url(self, href: str, data: str | None = None) -> FakeEvent:
        del data
        resource = next(
            (item for item in self.events_by_uid.values() if item.url == str(href)),
            None,
        )
        if resource is None:
            raise NotFoundError(str(href))
        return resource.load()

    def search(
        self,
        *,
        event: bool,
        start: datetime,
        end: datetime,
        expand: bool,
        split_expanded: bool,
        uid: str | None = None,
    ) -> list[FakeEvent]:
        assert event and expand and split_expanded
        if uid is not None:
            self.uid_queries += 1
        resources = []
        for resource in self.events_by_uid.values():
            if uid is not None and resource.id != uid:
                continue
            component = resource.get_icalendar_component()
            if component.get("RRULE") is not None:
                expanded = DAVEvent(data=resource.data)
                expanded.expand_rrule(start, end)
                for occurrence in expanded.icalendar_instance.walk("VEVENT"):
                    resources.append(
                        FakeEvent(
                            self,
                            _calendar_with(occurrence),
                            etag=resource.etag,
                            fragment=True,
                            url=resource.url,
                        )
                    )
                continue
            event_start, event_end = event_interval(component)
            if _as_datetime(event_end) > start and _as_datetime(event_start) < end:
                resources.append(resource)
        return resources


class FakePrincipal:
    def __init__(self, calendars: list[FakeCalendar]) -> None:
        self.calendars = calendars

    def get_calendars(self) -> list[FakeCalendar]:
        return self.calendars


class FakeDAV:
    def __init__(self, calendars: list[FakeCalendar], **kwargs: object) -> None:
        assert kwargs["url"] == "https://caldav.icloud.com/"
        assert kwargs["username"] == "person@example.com"
        assert kwargs["password"] == "password"
        self.principal = FakePrincipal(calendars)
        self.closed = False

    def get_principal(self) -> FakePrincipal:
        return self.principal

    def close(self) -> None:
        self.closed = True


def service(*calendars: FakeCalendar, default: str | None = None) -> ICloudCalendar:
    return ICloudCalendar(
        "person@example.com",
        "password",
        timezone="Europe/London",
        default_calendar=default,
        client_factory=lambda **kwargs: FakeDAV(list(calendars), **kwargs),
    )


def test_calendar_ids_are_opaque_and_reject_malformed_values() -> None:
    reference = EventReference("https://caldav.icloud.test/personal/", "uid-1")
    href_reference = EventReference(
        "https://caldav.icloud.test/personal/",
        "uid-1",
        resource_url="https://caldav.icloud.test/personal/server-name.ics",
    )

    assert decode_event_id(encode_event_id(reference)) == reference
    assert decode_event_id(encode_event_id(href_reference)) == href_reference
    with pytest.raises(CalendarError, match="not valid"):
        decode_event_id("calendar-event:not-base64")


def test_list_calendars_marks_the_configured_default_and_event_support() -> None:
    personal = FakeCalendar("Personal", "personal")
    reminders = FakeCalendar("Reminders", "reminders", supports_events=False)

    result = service(personal, reminders, default="Personal").list_calendars()

    assert [item["name"] for item in result["calendars"]] == [
        "Personal",
        "Reminders",
    ]
    assert result["calendars"][0]["is_default"] is True
    assert result["calendars"][1]["supports_events"] is False
    assert result["timezone"] == "Europe/London"


def test_create_search_read_update_and_delete_an_event() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)

    created = client.create_event(
        title="Project review",
        start="2026-09-02T09:00:00",
        end="2026-09-02T10:00:00",
        description="First draft",
        attendees=["Colleague@example.com"],
        alarms_minutes_before=[30, 10],
    )
    found = client.search_events("2026-09-01", "2026-09-03", query="project colleague")
    read = client.read_event(created["id"])
    updated = client.update_event(
        created["id"],
        expected_etag=read["etag"],
        title="Final project review",
        start="2026-09-02T11:00:00",
        description="",
        location="Conference room",
        alarms_minutes_before=[],
    )
    deleted = client.delete_event(updated["id"], expected_etag=updated["etag"])

    assert created["timezone"] == "Europe/London"
    assert created["attendees"][0]["email"] == "colleague@example.com"
    assert [event["id"] for event in found["events"]] == [created["id"]]
    assert updated["title"] == "Final project review"
    assert updated["start"] == "2026-09-02T11:00:00+01:00"
    assert updated["end"] == "2026-09-02T12:00:00+01:00"
    assert updated["description"] is None
    assert updated["location"] == "Conference room"
    assert updated["alarms"] == []
    assert deleted["scope"] == "series"
    assert calendar.events_by_uid == {}


def test_create_bypasses_icloud_incompatible_no_overwrite_preflight() -> None:
    class ICloudCalendar(FakeCalendar):
        def add_event(self, *, ical: str, no_overwrite: bool = False) -> FakeEvent:
            if no_overwrite:
                raise PutError("412 Precondition Failed\n\n")
            return super().add_event(ical=ical)

    client = service(ICloudCalendar("Personal", "personal"))

    created = client.create_event(
        title="Project review",
        start="2026-09-02T09:00:00",
        end="2026-09-02T10:00:00",
    )

    assert created["title"] == "Project review"


def test_event_operations_bypass_icloud_incompatible_uid_reports() -> None:
    class ICloudCalendar(FakeCalendar):
        def get_event_by_uid(self, uid: str) -> FakeEvent:
            self.uid_queries += 1
            raise ReportError(f"412 Precondition Failed while finding {uid}")

    calendar = ICloudCalendar("Personal", "personal")
    client = service(calendar)
    created = client.create_event(
        title="Temporary probe",
        start="2026-09-02T09:00:00",
        end="2026-09-02T10:00:00",
    )
    legacy_id = encode_event_id(EventReference(calendar.url, created["uid"]))

    read = client.read_event(legacy_id)
    updated = client.update_event(
        legacy_id, expected_etag=read["etag"], title="Updated probe"
    )
    deleted = client.delete_event(legacy_id, expected_etag=updated["etag"])

    assert read["title"] == "Temporary probe"
    assert updated["title"] == "Updated probe"
    assert deleted["status"] == "deleted"
    assert calendar.uid_queries == 0
    assert calendar.events_by_uid == {}


def test_event_ids_retain_an_opaque_server_resource_href() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)
    created = client.create_event(
        title="Server-named resource",
        start="2026-09-02T09:00:00",
        end="2026-09-02T10:00:00",
    )
    resource = calendar.events_by_uid[created["uid"]]
    resource.url = f"{calendar.url}opaque-server-name.ics"

    found = client.search_events("2026-09-01", "2026-09-03")["events"][0]
    reference = decode_event_id(found["id"])

    assert reference.resource_url == resource.url
    assert client.read_event(found["id"])["title"] == "Server-named resource"


def test_event_ids_cannot_address_resources_outside_the_calendar() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)
    forged = encode_event_id(
        EventReference(
            calendar.url,
            "uid-1",
            resource_url="https://caldav.icloud.test/calendars/other/event.ics",
        )
    )

    with pytest.raises(CalendarError, match="event id is not valid"):
        client.read_event(forged)


def test_event_ids_cannot_substitute_another_events_resource_href() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)
    first = client.create_event(
        title="First",
        start="2026-09-02T09:00:00",
        end="2026-09-02T10:00:00",
    )
    second = client.create_event(
        title="Second",
        start="2026-09-02T11:00:00",
        end="2026-09-02T12:00:00",
    )
    second_resource = calendar.events_by_uid[second["uid"]]
    forged = encode_event_id(
        EventReference(
            calendar.url,
            first["uid"],
            resource_url=second_resource.url,
        )
    )

    with pytest.raises(CalendarError, match="event id is not valid"):
        client.read_event(forged)


def test_all_day_events_keep_exclusive_date_boundaries() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)

    created = client.create_event(
        title="Conference", start="2026-09-10", end="2026-09-13"
    )
    moved = client.update_event(created["id"], start="2026-09-17")

    assert created["all_day"] is True
    assert created["timezone"] is None
    assert moved["start"] == "2026-09-17"
    assert moved["end"] == "2026-09-20"


def test_an_event_can_use_a_timezone_other_than_the_configured_default() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)

    created = client.create_event(
        title="New York meeting",
        start="2026-11-10T09:00:00-05:00",
        end="2026-11-10T10:00:00-05:00",
        timezone="America/New_York",
    )
    moved = client.update_event(created["id"], start="2026-11-10T11:00:00")

    assert created["timezone"] == "America/New_York"
    assert created["start"] == "2026-11-10T09:00:00-05:00"
    assert moved["timezone"] == "America/New_York"
    assert moved["end"] == "2026-11-10T12:00:00-05:00"


def test_update_rejects_an_etag_from_an_older_read() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)
    event = client.create_event(
        title="Original",
        start="2026-09-02T09:00:00+01:00",
        end="2026-09-02T10:00:00+01:00",
    )
    calendar.events_by_uid[event["uid"]]._advance_etag()

    with pytest.raises(CalendarConflict, match="changed since"):
        client.update_event(
            event["id"], expected_etag=event["etag"], title="Stale update"
        )


def test_respond_to_an_invitation_updates_the_accounts_attendee_status() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)
    event = client.create_event(
        title="Invited meeting",
        start="2026-09-02T09:00:00",
        end="2026-09-02T10:00:00",
        attendees=["person@example.com"],
    )

    result = client.respond_to_invitation(event["id"], "accepted")
    updated = client.read_event(event["id"])

    assert result == {"status": "accepted", "id": event["id"]}
    assert updated["attendees"][0]["status"] == "ACCEPTED"


@pytest.mark.filterwarnings("ignore:obj.expand_rrule is likely to be removed")
def test_recurring_search_returns_occurrences_and_can_delete_only_one() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)
    series = client.create_event(
        title="Weekly planning",
        start="2026-09-01T09:00:00",
        end="2026-09-01T10:00:00",
        recurrence="FREQ=WEEKLY;COUNT=3",
    )
    before = client.search_events("2026-09-01", "2026-09-30")
    middle = before["events"][1]

    updated = client.update_event(middle["id"], title="Special planning")
    changed = client.search_events("2026-09-01", "2026-09-30")
    deleted = client.delete_event(updated["id"])
    after = client.search_events("2026-09-01", "2026-09-30")

    assert [event["recurrence_id"] for event in before["events"]] == [
        "2026-09-01T09:00:00+01:00",
        "2026-09-08T09:00:00+01:00",
        "2026-09-15T09:00:00+01:00",
    ]
    assert all(event["series_id"] == series["id"] for event in before["events"])
    assert [event["title"] for event in changed["events"]] == [
        "Weekly planning",
        "Special planning",
        "Weekly planning",
    ]
    assert deleted["scope"] == "occurrence"
    assert calendar.uid_queries == 0
    assert [event["recurrence_id"] for event in after["events"]] == [
        "2026-09-01T09:00:00+01:00",
        "2026-09-15T09:00:00+01:00",
    ]


def test_free_busy_merges_overlaps_and_ignores_transparent_events() -> None:
    calendar = FakeCalendar("Personal", "personal")
    client = service(calendar)
    client.create_event(
        title="One",
        start="2026-09-02T09:00:00",
        end="2026-09-02T10:30:00",
    )
    client.create_event(
        title="Two",
        start="2026-09-02T10:00:00",
        end="2026-09-02T11:00:00",
    )
    client.create_event(
        title="FYI",
        start="2026-09-02T12:00:00",
        end="2026-09-02T13:00:00",
        busy=False,
    )

    result = client.free_busy("2026-09-02", "2026-09-03")

    assert len(result["busy"]) == 1
    assert result["busy"][0]["start"] == "2026-09-02T09:00:00+01:00"
    assert result["busy"][0]["end"] == "2026-09-02T11:00:00+01:00"
    assert result["busy"][0]["titles"] == ["One", "Two"]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-09-02", "2026-09-02T10:00:00", "both be dates"),
        ("2026-09-02T10:00:00", "2026-09-02T09:00:00", "later"),
    ],
)
def test_create_validates_event_intervals(start: str, end: str, message: str) -> None:
    with pytest.raises(CalendarError, match=message):
        service(FakeCalendar("Personal", "personal")).create_event(
            title="Invalid", start=start, end=end
        )
