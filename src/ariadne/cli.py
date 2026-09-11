"""A bounded, JSON-first command line for Ariadne capabilities."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Never, Protocol, cast
from uuid import UUID

from caldav.lib.error import (
    AuthorizationError,
    DAVError,
    ETagMismatchError,
    RateLimitError,
)
from imapclient import IMAPClient  # type: ignore[import-untyped]
from imapclient.exceptions import (  # type: ignore[import-untyped]
    IMAPClientError,
    LoginError,
)
from pydantic import ValidationError

from .calendar import CalendarConflict, CalendarError, ICloudCalendar
from .config import (
    MailAccountSettings,
    Settings,
    config_path,
    load_settings,
    settings_payload,
)
from .health import (
    IthacaAuthenticationError,
    IthacaClient,
    IthacaError,
    IthacaNotFoundError,
    IthacaRequestError,
    IthacaResponseError,
    IthacaUnavailableError,
    WorkoutActivityType,
)
from .health.presentation import HealthQueries
from .mail import (
    IMAP_HOST,
    DeviceAuthorization,
    MailAccountRegistry,
    MailAuthenticationError,
    MailClassifier,
    MailDrafts,
    MailReader,
    MailUnavailableError,
    MultiAccountMailReader,
    OutlookAuthorizationRequired,
    OutlookOAuth,
    connect_account,
    load_routes,
    validated_address,
)
from .redaction import redact_sensitive_text

EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_CONFLICT = 5
EXIT_TRANSIENT = 6
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ERROR_MESSAGE_LENGTH = 1_000


class MailCommands(Protocol):
    def search(
        self,
        query: str,
        *,
        since: str | None = None,
        before: str | None = None,
        limit: int = 20,
        account: str | None = None,
    ) -> dict[str, Any]: ...

    def read(self, value: str) -> dict[str, Any]: ...

    def read_thread(self, value: str) -> dict[str, Any]: ...


class MailDraftCommands(Protocol):
    def create_draft(
        self,
        *,
        body: str,
        reply_to: str | None = None,
        account: str | None = None,
        to: Sequence[str] = (),
        cc: Sequence[str] = (),
        subject: str | None = None,
    ) -> dict[str, Any]: ...


class MailClassifyCommands(Protocol):
    def classify(
        self, *, mail_id: str | None = None, path: Path | None = None
    ) -> dict[str, Any]: ...


class CalendarCommands(Protocol):
    def list_calendars(self) -> dict[str, Any]: ...

    def search_events(
        self,
        start: str,
        end: str,
        *,
        query: str | None = None,
        calendar_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]: ...

    def read_event(self, value: str) -> dict[str, Any]: ...

    def free_busy(
        self, start: str, end: str, *, calendar_ids: list[str] | None = None
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    def delete_event(
        self,
        value: str,
        *,
        scope: str = "occurrence",
        expected_etag: str | None = None,
    ) -> dict[str, Any]: ...

    def respond_to_invitation(
        self,
        value: str,
        response: str,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]: ...


class HealthCommands(Protocol):
    def list_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def summarize_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
    ) -> dict[str, Any]: ...

    def show_workout(self, workout_uuid: UUID) -> dict[str, Any]: ...

    def list_sleep(
        self,
        *,
        start_date: date,
        end_date: date,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def summarize_sleep(
        self, *, start_date: date, end_date: date
    ) -> dict[str, Any]: ...

    def show_sleep(self, sleep_date: date) -> dict[str, Any]: ...

    def latest_sleep(self) -> dict[str, Any]: ...


class CliBackend(Protocol):
    def config_check(self) -> dict[str, object]: ...

    def config_show(self) -> dict[str, Any]: ...

    def serve(self) -> None: ...

    def mail(self) -> AbstractContextManager[MailCommands]: ...

    def mail_drafts(self) -> AbstractContextManager[MailDraftCommands]: ...

    def mail_classification(self) -> AbstractContextManager[MailClassifyCommands]: ...

    def authorize_outlook(self) -> dict[str, object]: ...

    def calendar(self) -> AbstractContextManager[CalendarCommands]: ...

    def health(self) -> AbstractContextManager[HealthCommands]: ...


class CliError(Exception):
    """A safe command failure with a stable machine-readable classification."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


def _emit_error(error: CliError) -> None:
    message = redact_sensitive_text(error.message).strip()[:MAX_ERROR_MESSAGE_LENGTH]
    payload = {
        "error": {
            "code": error.code,
            "message": message,
            "retryable": error.retryable,
        }
    }
    print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=sys.stderr
    )


class AriadneArgumentParser(argparse.ArgumentParser):
    """Use argparse's familiar help while keeping failures machine-readable."""

    def error(self, message: str) -> Never:
        _emit_error(CliError("invalid_arguments", message, EXIT_USAGE))
        raise SystemExit(EXIT_USAGE)


class ProductionBackend:
    """Open configured providers only for the selected command."""

    def __init__(self, selected_config_path: Path | None) -> None:
        self.selected_config_path = selected_config_path
        self._settings: Settings | None = None

    def _load(self) -> Settings:
        if self._settings is None:
            try:
                self._settings = load_settings(self.selected_config_path)
            except (ValidationError, ValueError) as error:
                raise CliError(
                    "configuration_error",
                    f"Ariadne's private configuration is invalid: {error}",
                    EXIT_USAGE,
                ) from error
        return self._settings

    def config_check(self) -> dict[str, object]:
        self._load()
        return {
            "status": "valid",
            "path": str(config_path(self.selected_config_path)),
        }

    def config_show(self) -> dict[str, Any]:
        return settings_payload(self._load())

    def serve(self) -> None:
        from .service import run

        run(self.selected_config_path)

    @contextmanager
    def mail(self) -> Iterator[MailCommands]:
        settings = self._load()
        configured = settings.mail_settings
        if configured is None:
            raise CliError(
                "mail_not_configured",
                "Mail is not enabled in Ariadne's private configuration.",
                EXIT_USAGE,
            )
        if (
            len(configured.accounts) == 1
            and configured.accounts[0].provider == "icloud"
        ):
            account = configured.accounts[0]
            try:
                with IMAPClient(IMAP_HOST, port=993, ssl=True, timeout=30) as client:
                    assert account.password is not None
                    client.login(account.address, account.password.get_secret_value())
                    reader = (
                        MailReader(client)
                        if account.label == "iCloud"
                        else MailReader(client, account.key, account.label)
                    )
                    yield reader
            except LoginError as error:
                raise CliError(
                    "mail_authentication_failed",
                    "iCloud rejected the configured Mail credentials.",
                    EXIT_AUTH,
                ) from error
            except (IMAPClientError, RuntimeError) as error:
                raise CliError(
                    "mail_unavailable",
                    "iCloud Mail could not complete that operation.",
                    EXIT_TRANSIENT,
                    retryable=True,
                ) from error
            return

        @contextmanager
        def connector(account: MailAccountSettings) -> Iterator[IMAPClient]:
            with connect_account(account, client_factory=IMAPClient) as client:
                yield client

        registry = MailAccountRegistry(configured.accounts, connector=connector)
        try:
            yield MultiAccountMailReader(registry)
        except MailAuthenticationError as error:
            raise CliError(
                "mail_authentication_failed",
                str(error),
                EXIT_AUTH,
            ) from error
        except (MailUnavailableError, IMAPClientError, RuntimeError) as error:
            raise CliError(
                "mail_unavailable",
                "Mail could not complete that operation.",
                EXIT_TRANSIENT,
                retryable=True,
            ) from error

    def _mail_registry(self) -> MailAccountRegistry:
        settings = self._load()
        configured = settings.mail_settings
        if configured is None:
            raise CliError(
                "mail_not_configured",
                "Mail is not enabled in Ariadne's private configuration.",
                EXIT_USAGE,
            )

        @contextmanager
        def connector(account: MailAccountSettings) -> Iterator[IMAPClient]:
            with connect_account(account, client_factory=IMAPClient) as client:
                yield client

        return MailAccountRegistry(configured.accounts, connector=connector)

    @contextmanager
    def mail_drafts(self) -> Iterator[MailDraftCommands]:
        yield MailDrafts(self._mail_registry())

    @contextmanager
    def mail_classification(self) -> Iterator[MailClassifyCommands]:
        registry = self._mail_registry()
        settings = self._load()
        configured = settings.mail_settings
        assert configured is not None
        yield MailClassifier(load_routes(configured.routes), registry)

    def authorize_outlook(self) -> dict[str, object]:
        settings = self._load()
        account = next(
            (item for item in settings.mail_accounts if item.provider == "outlook"),
            None,
        )
        if account is None:
            raise CliError(
                "outlook_not_configured",
                "Outlook Mail is not enabled in Ariadne's private configuration.",
                EXIT_USAGE,
            )

        def notify(details: DeviceAuthorization) -> None:
            print(
                f"Open {details.verification_uri} on any device and enter "
                f"code {details.user_code}. Waiting for authorization…",
                file=sys.stderr,
                flush=True,
            )

        try:
            OutlookOAuth(account).authorize(notify)
        except RuntimeError as error:
            raise CliError(
                "outlook_authorization_failed", str(error), EXIT_AUTH
            ) from error
        return {
            "status": "authorized",
            "account_key": account.key,
            "account_label": account.label,
            "token_cache": str(account.token_cache),
        }

    @contextmanager
    def calendar(self) -> Iterator[CalendarCommands]:
        settings = self._load()
        if not settings.calendar.enabled or settings.icloud_credentials is None:
            raise CliError(
                "calendar_not_configured",
                "Calendar is not enabled in Ariadne's private configuration.",
                EXIT_USAGE,
            )
        username, password = settings.icloud_credentials
        with ICloudCalendar(
            username,
            password.get_secret_value(),
            timezone=settings.calendar.timezone,
            default_calendar=settings.calendar.default_calendar,
        ) as calendar:
            # argparse constrains the string literals before this typed boundary.
            yield cast(CalendarCommands, calendar)

    @contextmanager
    def health(self) -> Iterator[HealthCommands]:
        settings = self._load()
        health = settings.health
        if not health.enabled or health.api_url is None or health.read_token is None:
            raise CliError(
                "health_not_configured",
                "Health access is not enabled in Ariadne's private configuration.",
                EXIT_USAGE,
            )
        yield HealthQueries(
            IthacaClient(
                str(health.api_url),
                health.read_token.get_secret_value(),
                timezone=health.timezone,
                timeout_seconds=health.timeout_seconds,
            )
        )


def _serve(_: argparse.Namespace, backend: CliBackend) -> None:
    backend.serve()


def _config_check(_: argparse.Namespace, backend: CliBackend) -> dict[str, object]:
    return backend.config_check()


def _config_show(_: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    return backend.config_show()


def _mail_search(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.mail() as mail:
        arguments: dict[str, Any] = {
            "since": args.since,
            "before": args.before,
            "limit": args.limit,
        }
        if args.account is not None:
            arguments["account"] = args.account
        return mail.search(args.query, **arguments)


def _mail_authorize_outlook(
    _: argparse.Namespace, backend: CliBackend
) -> dict[str, object]:
    return backend.authorize_outlook()


def _mail_draft(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    body = _draft_body(args)
    if args.reply_to is not None:
        if args.to or args.cc or args.subject is not None or args.account is not None:
            raise CliError(
                "invalid_arguments",
                "A reply draft takes its account, recipients, and subject from the "
                "message it answers.",
                EXIT_USAGE,
            )
    elif not args.to or args.subject is None:
        raise CliError(
            "invalid_arguments",
            "A new draft needs --subject and at least one --to recipient.",
            EXIT_USAGE,
        )
    with backend.mail_drafts() as drafts:
        return drafts.create_draft(
            body=body,
            reply_to=args.reply_to,
            account=args.account,
            to=args.to or (),
            cc=args.cc or (),
            subject=args.subject,
        )


def _mail_classify(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.mail_classification() as classifier:
        return classifier.classify(mail_id=args.id, path=args.file)


def _mail_read(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.mail() as mail:
        return mail.read(args.id)


def _mail_thread(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.mail() as mail:
        return mail.read_thread(args.id)


def _calendar_list(_: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.list_calendars()


def _calendar_search(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.search_events(
            args.start,
            args.end,
            query=args.query,
            calendar_ids=args.calendar_ids,
            limit=args.limit,
        )


def _calendar_read(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.read_event(args.id)


def _calendar_availability(
    args: argparse.Namespace, backend: CliBackend
) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.free_busy(
            args.start,
            args.end,
            calendar_ids=args.calendar_ids,
        )


def _calendar_create(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.create_event(
            title=args.title,
            start=args.start,
            end=args.end,
            calendar_id=args.calendar_id,
            description=args.description,
            location=args.location,
            attendees=args.attendees,
            timezone=args.timezone,
            recurrence=args.recurrence,
            alarms_minutes_before=args.alarms,
            status=args.status,
            busy=args.busy,
        )


def _calendar_update(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.update_event(
            args.id,
            scope=args.scope,
            expected_etag=args.expected_etag,
            title=args.title,
            start=args.start,
            end=args.end,
            description=args.description,
            location=args.location,
            attendees=args.attendees,
            timezone=args.timezone,
            recurrence=args.recurrence,
            alarms_minutes_before=args.alarms,
            status=args.status,
            busy=args.busy,
        )


def _calendar_delete(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.delete_event(
            args.id,
            scope=args.scope,
            expected_etag=args.expected_etag,
        )


def _calendar_respond(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.calendar() as calendar:
        return calendar.respond_to_invitation(
            args.id,
            args.response,
            expected_etag=args.expected_etag,
        )


def _health_workouts_list(
    args: argparse.Namespace, backend: CliBackend
) -> dict[str, Any]:
    with backend.health() as health:
        return health.list_workouts(
            start=args.start,
            end=args.end,
            activity_types=args.activity_types or (),
            limit=args.limit,
            cursor=args.cursor,
        )


def _health_workouts_summarize(
    args: argparse.Namespace, backend: CliBackend
) -> dict[str, Any]:
    with backend.health() as health:
        return health.summarize_workouts(
            start=args.start,
            end=args.end,
            activity_types=args.activity_types or (),
        )


def _health_workouts_show(
    args: argparse.Namespace, backend: CliBackend
) -> dict[str, Any]:
    with backend.health() as health:
        return health.show_workout(args.workout_id)


def _health_sleep_list(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.health() as health:
        return health.list_sleep(
            start_date=args.start_date,
            end_date=args.end_date,
            limit=args.limit,
            cursor=args.cursor,
        )


def _health_sleep_summarize(
    args: argparse.Namespace, backend: CliBackend
) -> dict[str, Any]:
    with backend.health() as health:
        return health.summarize_sleep(
            start_date=args.start_date,
            end_date=args.end_date,
        )


def _health_sleep_show(args: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.health() as health:
        return health.show_sleep(args.sleep_date)


def _health_sleep_latest(_: argparse.Namespace, backend: CliBackend) -> dict[str, Any]:
    with backend.health() as health:
        return health.latest_sleep()


def _add_calendar_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--calendar-id",
        dest="calendar_ids",
        action="append",
        metavar="ID",
        help="opaque calendar id; repeat to select more than one",
    )


def _add_calendar_write_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", help="replacement event title")
    parser.add_argument("--start", help="replacement ISO date or date-time")
    parser.add_argument("--end", help="replacement ISO date or date-time")
    description = parser.add_mutually_exclusive_group()
    description.add_argument("--description", help="replacement description")
    description.add_argument(
        "--clear-description",
        dest="description",
        action="store_const",
        const="",
        help="remove the description",
    )
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--location", help="replacement location")
    location.add_argument(
        "--clear-location",
        dest="location",
        action="store_const",
        const="",
        help="remove the location",
    )
    attendees = parser.add_mutually_exclusive_group()
    attendees.add_argument(
        "--attendee",
        dest="attendees",
        action="append",
        metavar="EMAIL",
        help="replacement attendee; repeat for more than one",
    )
    attendees.add_argument(
        "--clear-attendees",
        dest="attendees",
        action="store_const",
        const=[],
        help="remove every attendee",
    )
    parser.add_argument("--timezone", help="IANA timezone for offset-free times")
    recurrence = parser.add_mutually_exclusive_group()
    recurrence.add_argument(
        "--recurrence", help="replacement RFC 5545 RRULE; requires --scope series"
    )
    recurrence.add_argument(
        "--clear-recurrence",
        dest="recurrence",
        action="store_const",
        const="",
        help="make the event non-recurring",
    )
    alarms = parser.add_mutually_exclusive_group()
    alarms.add_argument(
        "--alarm-minutes-before",
        dest="alarms",
        action="append",
        type=int,
        metavar="MINUTES",
        help="replacement alarm, 0-525600; repeat for more than one",
    )
    alarms.add_argument(
        "--clear-alarms",
        dest="alarms",
        action="store_const",
        const=[],
        help="remove every alarm",
    )
    parser.add_argument(
        "--status",
        choices=("confirmed", "tentative", "cancelled"),
        help="replacement event status",
    )


def _package_version() -> str:
    try:
        return version("ariadne")
    except PackageNotFoundError:
        return "unknown"


def _bounded_integer(minimum: int, maximum: int) -> Callable[[str], int]:
    """Return an argparse converter with a short, actionable range error."""

    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return parsed

    return parse


def _bounded_text(maximum: int, label: str) -> Callable[[str], str]:
    def parse(value: str) -> str:
        if not value or len(value) > maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must contain between 1 and {maximum} characters"
            )
        return value

    return parse


def _mail_address(value: str) -> str:
    try:
        return validated_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _draft_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return cast(str, args.body)
    path = cast(Path, args.body_file)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CliError(
            "invalid_arguments",
            f"The draft body file could not be read: {path}",
            EXIT_USAGE,
        ) from error


def _workout_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "must be a workout ID returned by list"
        ) from error


def _workout_activity(value: str) -> WorkoutActivityType:
    try:
        return WorkoutActivityType(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "unknown activity type; see this command's --help for valid values"
        ) from error


def _sleep_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO date (YYYY-MM-DD)") from error


def _add_workout_period(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--start",
        required=True,
        help="inclusive ISO date or date-time (date-only uses health timezone)",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="exclusive ISO date or date-time (date-only uses health timezone)",
    )
    parser.add_argument(
        "--activity",
        dest="activity_types",
        action="append",
        type=_workout_activity,
        metavar="TYPE",
        help="workout activity type; repeat to include more than one",
    )


def _add_sleep_period(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--start",
        dest="start_date",
        type=_sleep_date,
        required=True,
        help="inclusive derived sleep date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        dest="end_date",
        type=_sleep_date,
        required=True,
        help="exclusive derived sleep date (YYYY-MM-DD)",
    )


def build_parser() -> AriadneArgumentParser:
    """Build help without loading configuration or contacting a provider."""
    parser = AriadneArgumentParser(
        prog="ariadne",
        description="Operate Ariadne and its configured personal-data capabilities.",
        epilog=(
            "Run 'ariadne <command> --help' and "
            "'ariadne <command> <subcommand> --help' for details. Data and config "
            "commands emit bounded JSON; their failures emit JSON on stderr."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="private Ariadne TOML path (must precede the command)",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="indent JSON output for humans"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {_package_version()}"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser(
        "serve",
        help="run Telegram and background loops",
        description="Run Telegram plus configured Mail-ingestion and revisit loops.",
    )
    serve.set_defaults(_handler=_serve)

    config = commands.add_parser("config", help="inspect typed configuration")
    config_actions = config.add_subparsers(dest="config_action", required=True)
    config_check = config_actions.add_parser(
        "check",
        help="validate configuration",
        description="Validate the selected private configuration without printing it.",
    )
    config_check.set_defaults(_handler=_config_check)
    config_show = config_actions.add_parser(
        "show",
        help="show redacted configuration",
        description="Print effective configuration with every secret value redacted.",
    )
    config_show.set_defaults(_handler=_config_show)

    mail = commands.add_parser(
        "mail", help="search, read, classify, and draft enabled Mail accounts"
    )
    mail_actions = mail.add_subparsers(dest="mail_action", required=True)
    mail_search = mail_actions.add_parser(
        "search",
        help="search mail by ordinary words, people, or topics",
        description=(
            "Search read-only mail previews and return opaque ids for read or thread."
        ),
    )
    mail_search.add_argument("query", help="words, person, company, or topic")
    mail_search.add_argument("--since", help="inclusive ISO date (YYYY-MM-DD)")
    mail_search.add_argument("--before", help="exclusive ISO date (YYYY-MM-DD)")
    mail_search.add_argument(
        "--account",
        help="stable account key to search instead of all enabled accounts",
    )
    mail_search.add_argument(
        "--limit",
        type=_bounded_integer(1, 100),
        default=20,
        metavar="N",
        help="maximum results, 1-100 (default: 20)",
    )
    mail_search.set_defaults(_handler=_mail_search)
    mail_read = mail_actions.add_parser(
        "read",
        help="read one opaque mail id",
        description="Read one full message without changing its read state.",
    )
    mail_read.add_argument("id", help="opaque id returned by mail search")
    mail_read.set_defaults(_handler=_mail_read)
    mail_thread = mail_actions.add_parser(
        "thread",
        help="read the bounded thread around one mail id",
        description="Read a bounded conversation around one mail search result.",
    )
    mail_thread.add_argument("id", help="opaque id returned by mail search")
    mail_thread.set_defaults(_handler=_mail_thread)
    mail_draft = mail_actions.add_parser(
        "draft",
        help="leave an unsent draft in the mailbox",
        description=(
            "Create an editable draft in the account's Drafts folder. Ariadne "
            "never sends mail."
        ),
        epilog=(
            "Reply to a message with --reply-to, or start a new one with --to and "
            "--subject. A reply is addressed to everyone on the original message; "
            "trim the recipients in your mail client before sending it yourself."
        ),
    )
    mail_draft.add_argument(
        "--reply-to", metavar="ID", help="opaque id returned by mail search"
    )
    mail_draft.add_argument(
        "--account",
        metavar="KEY",
        help="account for a new draft; required when several are enabled",
    )
    mail_draft.add_argument(
        "--to",
        action="append",
        metavar="EMAIL",
        type=_mail_address,
        help="new-draft recipient; repeat for more than one",
    )
    mail_draft.add_argument(
        "--cc",
        action="append",
        metavar="EMAIL",
        type=_mail_address,
        help="new-draft copy recipient; repeat for more than one",
    )
    mail_draft.add_argument(
        "--subject",
        type=_bounded_text(500, "subject"),
        help="new-draft subject",
    )
    draft_body = mail_draft.add_mutually_exclusive_group(required=True)
    draft_body.add_argument(
        "--body", type=_bounded_text(100_000, "body"), help="draft body text"
    )
    draft_body.add_argument(
        "--body-file", type=Path, metavar="PATH", help="UTF-8 file holding the body"
    )
    mail_draft.set_defaults(_handler=_mail_draft)

    mail_classify = mail_actions.add_parser(
        "classify",
        help="report how mail routing would handle one message",
        description=(
            "Run the ingestion routing rules against one message without moving, "
            "marking, filing, or waking Iris."
        ),
    )
    classify_source = mail_classify.add_mutually_exclusive_group(required=True)
    classify_source.add_argument("--id", help="opaque id returned by mail search")
    classify_source.add_argument(
        "--file", type=Path, metavar="PATH", help="raw RFC 822 message file"
    )
    mail_classify.set_defaults(_handler=_mail_classify)

    mail_authorize = mail_actions.add_parser(
        "authorize-outlook",
        help="authorize personal Outlook.com Mail using a browser on any device",
        description=(
            "Print Microsoft's device URL and code, then save a refreshable token "
            "cache privately on this Ariadne host."
        ),
    )
    mail_authorize.set_defaults(_handler=_mail_authorize_outlook)

    calendar = commands.add_parser("calendar", help="read and change iCloud Calendar")
    calendar_actions = calendar.add_subparsers(dest="calendar_action", required=True)
    calendar_list = calendar_actions.add_parser(
        "list",
        help="list calendars",
        description="List calendars and return opaque ids accepted by other commands.",
    )
    calendar_list.set_defaults(_handler=_calendar_list)
    calendar_search = calendar_actions.add_parser(
        "search",
        help="search a bounded calendar interval",
        description=(
            "Search events overlapping an interval; recurring events are expanded."
        ),
    )
    calendar_search.add_argument("--start", required=True, help="ISO date or date-time")
    calendar_search.add_argument("--end", required=True, help="ISO date or date-time")
    calendar_search.add_argument(
        "--query", help="words matched across title, description, location, attendees"
    )
    _add_calendar_selection(calendar_search)
    calendar_search.add_argument(
        "--limit",
        type=_bounded_integer(1, 200),
        default=50,
        metavar="N",
        help="maximum events, 1-200 (default: 50)",
    )
    calendar_search.set_defaults(_handler=_calendar_search)
    calendar_read = calendar_actions.add_parser(
        "read",
        help="read one opaque event id",
        description="Read one full event or expanded recurrence occurrence.",
    )
    calendar_read.add_argument("id", help="opaque id returned by calendar search")
    calendar_read.set_defaults(_handler=_calendar_read)
    availability = calendar_actions.add_parser(
        "availability",
        help="return merged busy intervals",
        description="Return merged busy intervals across selected calendars.",
    )
    availability.add_argument("--start", required=True, help="ISO date or date-time")
    availability.add_argument("--end", required=True, help="ISO date or date-time")
    _add_calendar_selection(availability)
    availability.set_defaults(_handler=_calendar_availability)

    create = calendar_actions.add_parser(
        "create",
        help="create an event immediately",
        description=(
            "Create an event immediately; attendee changes may send invitations."
        ),
    )
    create.add_argument("--title", required=True, help="event title")
    create.add_argument(
        "--start",
        required=True,
        help="ISO date or date-time; a date creates an all-day event",
    )
    create.add_argument(
        "--end",
        required=True,
        help="ISO date or date-time; an all-day date end is exclusive",
    )
    create.add_argument(
        "--calendar-id", help="opaque destination id from calendar list"
    )
    create.add_argument("--description", help="event description")
    create.add_argument("--location", help="event location")
    create.add_argument(
        "--attendee",
        dest="attendees",
        action="append",
        metavar="EMAIL",
        help="attendee email; repeat for more than one",
    )
    create.add_argument("--timezone", help="IANA timezone for offset-free times")
    create.add_argument("--recurrence", help="one RFC 5545 RRULE value")
    create.add_argument(
        "--alarm-minutes-before",
        dest="alarms",
        action="append",
        type=int,
        metavar="MINUTES",
        help="alarm lead time, 0-525600; repeat for more than one",
    )
    create.add_argument(
        "--status",
        choices=("confirmed", "tentative", "cancelled"),
        default="confirmed",
        help="event status (default: confirmed)",
    )
    create.add_argument(
        "--free", dest="busy", action="store_false", help="do not block availability"
    )
    create.set_defaults(_handler=_calendar_create)

    update = calendar_actions.add_parser(
        "update",
        help="patch an event immediately",
        description=(
            "Patch only supplied fields; clear flags intentionally remove values."
        ),
    )
    update.add_argument("id", help="opaque id returned by calendar search")
    update.add_argument(
        "--scope",
        choices=("occurrence", "series"),
        default="occurrence",
        help="target one occurrence or the whole series (default: occurrence)",
    )
    update.add_argument(
        "--expected-etag", help="reject the write unless this last-read ETag matches"
    )
    _add_calendar_write_common(update)
    busy = update.add_mutually_exclusive_group()
    busy.add_argument(
        "--busy", dest="busy", action="store_true", help="block availability"
    )
    busy.add_argument(
        "--free", dest="busy", action="store_false", help="do not block availability"
    )
    update.set_defaults(busy=None, _handler=_calendar_update)

    delete = calendar_actions.add_parser(
        "delete",
        help="delete an event immediately",
        description="Delete one event, occurrence, or recurring series immediately.",
    )
    delete.add_argument("id", help="opaque id returned by calendar search")
    delete.add_argument(
        "--scope",
        choices=("occurrence", "series"),
        default="occurrence",
        help="delete one occurrence or the whole series (default: occurrence)",
    )
    delete.add_argument(
        "--expected-etag", help="reject the delete unless this last-read ETag matches"
    )
    delete.set_defaults(_handler=_calendar_delete)

    respond = calendar_actions.add_parser(
        "respond",
        help="respond to an event invitation immediately",
        description="Accept, tentatively accept, or decline an invitation immediately.",
    )
    respond.add_argument("id", help="opaque id returned by calendar search")
    respond.add_argument(
        "response",
        choices=("accepted", "tentative", "declined"),
        help="invitation response",
    )
    respond.add_argument(
        "--expected-etag", help="reject the write unless this last-read ETag matches"
    )
    respond.set_defaults(_handler=_calendar_respond)

    health = commands.add_parser(
        "health",
        help="read factual personal health data",
        description="Read bounded workout and sleep history from Ithaca.",
        epilog="Run 'ariadne health <category> --help' for available queries.",
    )
    health_actions = health.add_subparsers(dest="health_action", required=True)
    workouts = health_actions.add_parser(
        "workouts",
        help="list and inspect workout history",
        description="Read compact, factual workout metrics from Ithaca.",
        epilog=(
            "List and summarize require --start and --end. Show accepts a "
            "WORKOUT_ID returned by list. Run 'ariadne health workouts <command> "
            "--help' for full options."
        ),
    )
    workout_actions = workouts.add_subparsers(dest="workout_action", required=True)
    activity_epilog = "Valid activity types:\n  " + ", ".join(
        activity.value for activity in WorkoutActivityType
    )

    workout_list = workout_actions.add_parser(
        "list",
        help="browse a bounded period newest first",
        description=(
            "List newest-first compact workouts and return workout IDs for show."
        ),
        epilog=activity_epilog,
    )
    _add_workout_period(workout_list)
    workout_list.add_argument(
        "--limit",
        type=_bounded_integer(1, 50),
        default=20,
        metavar="N",
        help="maximum workouts, 1-50 (default: 20)",
    )
    workout_list.add_argument(
        "--cursor",
        type=_bounded_text(2048, "cursor"),
        help="opaque next_cursor from a matching earlier list",
    )
    workout_list.set_defaults(_handler=_health_workouts_list)

    workout_summarize = workout_actions.add_parser(
        "summarize",
        help="calculate factual totals for a bounded period",
        description=(
            "Return factual period totals and complete activity-specific metrics."
        ),
        epilog=activity_epilog,
    )
    _add_workout_period(workout_summarize)
    workout_summarize.set_defaults(_handler=_health_workouts_summarize)

    workout_show = workout_actions.add_parser(
        "show",
        help="inspect one compact workout",
        description=(
            "Show metrics, splits, zones, components, route availability, quality, "
            "and data freshness for one workout."
        ),
    )
    workout_show.add_argument(
        "workout_id",
        type=_workout_id,
        metavar="WORKOUT_ID",
        help="workout ID returned by list",
    )
    workout_show.set_defaults(_handler=_health_workouts_show)

    sleep = health_actions.add_parser(
        "sleep",
        help="list and inspect sleep history",
        description="Read compact, factual sleep-day records from Ithaca.",
        epilog=(
            "List and summarize require --start and --end. Show accepts a "
            "SLEEP_DATE returned by list or latest. A sleep date is the local date "
            "on which an episode ended, using its recorded timezone."
        ),
    )
    sleep_actions = sleep.add_subparsers(dest="sleep_action", required=True)

    sleep_list = sleep_actions.add_parser(
        "list",
        help="browse a bounded date range newest first",
        description=(
            "List newest-first compact sleep days and return dates accepted by show."
        ),
    )
    _add_sleep_period(sleep_list)
    sleep_list.add_argument(
        "--limit",
        type=_bounded_integer(1, 50),
        default=20,
        metavar="N",
        help="maximum sleep days, 1-50 (default: 20)",
    )
    sleep_list.add_argument(
        "--cursor",
        type=_bounded_text(2048, "cursor"),
        help="opaque next_cursor from a matching earlier list",
    )
    sleep_list.set_defaults(_handler=_health_sleep_list)

    sleep_summarize = sleep_actions.add_parser(
        "summarize",
        help="calculate factual sleep trends for a bounded range",
        description=(
            "Return observed-day counts plus duration and local-timing averages and "
            "variability."
        ),
    )
    _add_sleep_period(sleep_summarize)
    sleep_summarize.set_defaults(_handler=_health_sleep_summarize)

    sleep_show = sleep_actions.add_parser(
        "show",
        help="inspect one sleep day",
        description=(
            "Show the main and additional sleep episodes with ordered stage intervals."
        ),
    )
    sleep_show.add_argument(
        "sleep_date",
        type=_sleep_date,
        metavar="SLEEP_DATE",
        help="derived sleep date returned by list or latest",
    )
    sleep_show.set_defaults(_handler=_health_sleep_show)

    sleep_latest = sleep_actions.add_parser(
        "latest",
        help="read the most recent compact sleep day",
        description="Return the most recent available sleep day without its timeline.",
    )
    sleep_latest.set_defaults(_handler=_health_sleep_latest)
    return parser


def _as_cli_error(error: Exception) -> CliError:
    if isinstance(error, CliError):
        return error
    if isinstance(error, OutlookAuthorizationRequired | MailAuthenticationError):
        return CliError("mail_authentication_failed", str(error), EXIT_AUTH)
    if isinstance(error, MailUnavailableError):
        return CliError("mail_unavailable", str(error), EXIT_TRANSIENT, retryable=True)
    if isinstance(error, IthacaAuthenticationError):
        return CliError(
            "health_authentication_failed",
            str(error),
            EXIT_AUTH,
        )
    if isinstance(error, IthacaNotFoundError):
        return CliError("health_not_found", str(error), EXIT_NOT_FOUND)
    if isinstance(error, IthacaRequestError):
        return CliError("health_invalid_request", str(error), EXIT_USAGE)
    if isinstance(error, IthacaUnavailableError):
        return CliError(
            "health_unavailable",
            str(error),
            EXIT_TRANSIENT,
            retryable=True,
        )
    if isinstance(error, IthacaResponseError):
        return CliError("health_invalid_response", str(error), EXIT_INTERNAL)
    if isinstance(error, IthacaError):
        return CliError(
            "health_error",
            "Ariadne could not complete that health read.",
            EXIT_INTERNAL,
        )
    if isinstance(error, ValidationError):
        return CliError(
            "configuration_error",
            f"Ariadne's private configuration is invalid: {error}",
            EXIT_USAGE,
        )
    if isinstance(error, CalendarConflict | ETagMismatchError):
        return CliError("calendar_conflict", str(error), EXIT_CONFLICT)
    if isinstance(error, CalendarError | ValueError):
        return CliError("invalid_request", str(error), EXIT_USAGE)
    if isinstance(error, AuthorizationError):
        return CliError(
            "calendar_authentication_failed",
            "iCloud rejected the configured Calendar credentials.",
            EXIT_AUTH,
        )
    if isinstance(error, RateLimitError):
        return CliError(
            "calendar_rate_limited",
            "iCloud Calendar is temporarily rate limited.",
            EXIT_TRANSIENT,
            retryable=True,
        )
    if isinstance(error, DAVError):
        return CliError(
            "calendar_unavailable",
            "iCloud Calendar could not complete that operation.",
            EXIT_TRANSIENT,
            retryable=True,
        )
    if isinstance(error, OSError):
        return CliError(
            "provider_unavailable",
            "The configured provider could not be reached.",
            EXIT_TRANSIENT,
            retryable=True,
        )
    return CliError(
        "internal_error",
        "Ariadne could not complete that command.",
        EXIT_INTERNAL,
    )


def _render(payload: object, *, pretty: bool) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    if len(rendered.encode("utf-8")) + 1 > MAX_OUTPUT_BYTES:
        raise CliError(
            "output_too_large",
            "The command result exceeded Ariadne's output limit; narrow the request.",
            EXIT_USAGE,
        )
    return rendered


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: CliBackend | None = None,
) -> None:
    """Parse and execute exactly one Ariadne command."""
    args = build_parser().parse_args(argv)
    selected_backend = backend or ProductionBackend(args.config)
    handler = cast(Any, args._handler)
    previous_logging_disable = logging.root.manager.disable
    if args.command != "serve":
        logging.disable(logging.CRITICAL)
    try:
        result = handler(args, selected_backend)
        if result is not None:
            print(_render(result, pretty=args.pretty))
    except Exception as error:
        failure = _as_cli_error(error)
        _emit_error(failure)
        raise SystemExit(failure.status) from error
    finally:
        logging.disable(previous_logging_disable)
