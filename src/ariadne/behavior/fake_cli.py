"""Disposable Mail and Calendar CLI used by manual behaviour scenarios."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Never, cast

from fastmcp.exceptions import ToolError

from ariadne.calendar import CalendarStatus, InvitationResponse, UpdateScope
from ariadne.cli import EXIT_USAGE, CliError
from ariadne.cli import main as run_cli

from . import fake_calendar
from .recording import record_call


class FakeMail:
    def search(
        self,
        query: str,
        *,
        since: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        record_call(
            "cli.mail.search",
            {"query": query, "since": since, "before": before, "limit": limit},
        )
        return {"query": query, "results": [], "searched_folders": 1}

    def read(self, value: str) -> dict[str, Any]:
        record_call("cli.mail.read", {"id": value})
        raise ToolError("That scenario mail id is not available.")

    def read_thread(self, value: str) -> dict[str, Any]:
        record_call("cli.mail.thread", {"id": value})
        raise ToolError("That scenario mail id is not available.")


class FakeCalendar:
    def list_calendars(self) -> dict[str, Any]:
        return fake_calendar.list_calendars()

    def search_events(
        self,
        start: str,
        end: str,
        *,
        query: str | None = None,
        calendar_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return fake_calendar.search_calendar_events(
            start,
            end,
            query=query,
            calendar_ids=calendar_ids,
            limit=limit,
        )

    def read_event(self, value: str) -> dict[str, Any]:
        return fake_calendar.read_calendar_event(value)

    def free_busy(
        self, start: str, end: str, *, calendar_ids: list[str] | None = None
    ) -> dict[str, Any]:
        return fake_calendar.check_calendar_availability(
            start, end, calendar_ids=calendar_ids
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
        status: str = "confirmed",
        busy: bool = True,
    ) -> dict[str, Any]:
        return fake_calendar.create_calendar_event(
            title,
            start,
            end,
            calendar_id=calendar_id,
            description=description,
            location=location,
            attendees=attendees,
            timezone=timezone,
            recurrence=recurrence,
            alarms_minutes_before=alarms_minutes_before,
            status=cast(CalendarStatus, status),
            busy=busy,
        )

    def update_event(
        self,
        value: str,
        *,
        scope: str = "occurrence",
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
        status: str | None = None,
        busy: bool | None = None,
    ) -> dict[str, Any]:
        return fake_calendar.update_calendar_event(
            value,
            scope=cast(UpdateScope, scope),
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
            status=cast(CalendarStatus | None, status),
            busy=busy,
        )

    def delete_event(
        self,
        value: str,
        *,
        scope: str = "occurrence",
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        return fake_calendar.delete_calendar_event(
            value,
            scope=cast(UpdateScope, scope),
            expected_etag=expected_etag,
        )

    def respond_to_invitation(
        self,
        value: str,
        response: str,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        return fake_calendar.respond_to_calendar_invitation(
            value,
            cast(InvitationResponse, response),
            expected_etag=expected_etag,
        )


class BehaviorBackend:
    """Provide only harmless scenario state to the shared CLI parser."""

    def _unsupported(self) -> Never:
        raise CliError(
            "unsupported_scenario_command",
            "That command is unavailable in a behaviour scenario.",
            EXIT_USAGE,
        )

    def config_check(self) -> dict[str, object]:
        self._unsupported()

    def config_show(self) -> dict[str, Any]:
        self._unsupported()

    def serve(self) -> None:
        self._unsupported()

    @contextmanager
    def mail(self) -> Iterator[FakeMail]:
        try:
            yield FakeMail()
        except ToolError as error:
            raise CliError("invalid_request", str(error), EXIT_USAGE) from error

    @contextmanager
    def calendar(self) -> Iterator[FakeCalendar]:
        try:
            yield FakeCalendar()
        except ToolError as error:
            raise CliError("invalid_request", str(error), EXIT_USAGE) from error


def main() -> None:
    run_cli(backend=BehaviorBackend())


if __name__ == "__main__":
    main()
