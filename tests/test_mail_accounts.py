from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr, ValidationError

import ariadne.cli as cli_module
import ariadne.mail.reader as reader_module
from ariadne.cli import ProductionBackend, main
from ariadne.config import MailAccountSettings, load_settings, settings_payload
from ariadne.mail import (
    DeviceAuthorization,
    MailAccountRegistry,
    MailLoop,
    MailProcessor,
    MailRoutes,
    MailState,
    MultiAccountMailReader,
    OutlookAuthorizationRequired,
    OutlookOAuth,
    authenticate_client,
    decode_mail_id,
    encode_mail_id,
    select_account,
)
from ariadne.mail.reader import MailReference


def _config(tmp_path: Path, extra: str) -> Path:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    routes = tmp_path / "routes.yaml"
    routes.write_text("version: 1\n", encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        f'''\
version = 1
human_name = "Example"
vault = "{vault}"

[telegram]
bot_token = "telegram-secret"
allowed_user_id = 7

{extra.replace("ROUTES", str(routes))}
''',
        encoding="utf-8",
    )
    return path


def _outlook(tmp_path: Path) -> MailAccountSettings:
    return MailAccountSettings(
        key="outlook",
        label="Personal Outlook",
        provider="outlook",
        address="owner@outlook.example",
        host="outlook.office365.com",
        client_id="public-client-id",
        token_cache=tmp_path / "private" / "outlook-token.json",
    )


def _icloud() -> MailAccountSettings:
    return MailAccountSettings(
        key="icloud",
        label="Personal iCloud",
        provider="icloud",
        address="owner@icloud.example",
        host="imap.mail.me.com",
        password=SecretStr("app-password"),
    )


def test_legacy_icloud_and_outlook_can_be_enabled_together(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
[icloud]
username = "owner@icloud.example"
app_password = "icloud-password"
mail_label = "Old inbox"

[outlook]
enabled = true
address = "owner@outlook.example"
client_id = "public-client-id"
token_cache = "~/private-outlook-token.json"
label = "New inbox"

[mail]
enabled = true
routes = "ROUTES"
""",
    )

    settings = load_settings(path, environ={})
    configured = settings.mail_settings

    assert configured is not None
    assert [(item.key, item.label) for item in configured.accounts] == [
        ("icloud", "Old inbox"),
        ("outlook", "New inbox"),
    ]
    assert (
        configured.accounts[1].token_cache
        == Path("~/private-outlook-token.json").expanduser().resolve()
    )
    serialized = json.dumps(settings_payload(settings))
    assert "icloud-password" not in serialized
    assert "telegram-secret" not in serialized


def test_outlook_can_be_the_only_enabled_mail_account(tmp_path: Path) -> None:
    settings = load_settings(
        _config(
            tmp_path,
            """\
[outlook]
enabled = true
address = "owner@outlook.example"
client_id = "public-client-id"

[mail]
enabled = true
routes = "ROUTES"
""",
        ),
        environ={},
    )

    assert settings.icloud_credentials is None
    assert settings.mail_settings is not None
    assert [item.key for item in settings.mail_settings.accounts] == ["outlook"]


def test_enabled_outlook_requires_address_and_public_client_id(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
[outlook]
enabled = true
address = "owner@outlook.example"

[mail]
enabled = true
routes = "ROUTES"
""",
    )

    with pytest.raises(ValidationError, match="address and client_id"):
        load_settings(path, environ={})


def test_versioned_ids_bind_account_and_legacy_ids_resolve_to_icloud() -> None:
    encoded = encode_mail_id(MailReference("Archive", 42, 7, "outlook"))

    assert decode_mail_id(encoded) == MailReference("Archive", 42, 7, "outlook")

    legacy_raw = json.dumps(["INBOX", 3, 9], separators=(",", ":")).encode()
    legacy = "mail:" + base64.urlsafe_b64encode(legacy_raw).decode().rstrip("=")
    assert decode_mail_id(legacy) == MailReference("INBOX", 3, 9, "icloud")


def test_bulk_operator_selection_refuses_an_ambiguous_account(tmp_path: Path) -> None:
    accounts = (_icloud(), _outlook(tmp_path))

    with pytest.raises(ValueError, match="pass --account"):
        select_account(accounts, None, require_explicit_when_multiple=True)

    assert (
        select_account(
            accounts, "outlook", require_explicit_when_multiple=True
        ).provider
        == "outlook"
    )


def test_device_authorization_uses_consumer_scopes_and_private_atomic_cache(
    tmp_path: Path,
) -> None:
    account = _outlook(tmp_path)
    calls: list[tuple[str, dict[str, str]]] = []
    responses: list[dict[str, Any]] = [
        {
            "device_code": "device-secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://microsoft.example/device",
            "message": "Use a browser",
            "expires_in": 900,
            "interval": 1,
        },
        {"error": "authorization_pending"},
        {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
        },
    ]

    def post(url: str, values: dict[str, str]) -> dict[str, Any]:
        calls.append((url, dict(values)))
        return responses.pop(0)

    shown = []
    oauth = OutlookOAuth(
        account, poster=post, clock=lambda: 100.0, sleep=lambda _: None
    )
    details = oauth.authorize(shown.append)

    assert shown == [details]
    assert details.user_code == "ABCD-EFGH"
    assert "/consumers/oauth2/v2.0/devicecode" in calls[0][0]
    assert calls[0][1]["scope"] == (
        "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
    )
    assert "client_secret" not in calls[0][1]
    assert account.token_cache is not None
    assert stat.S_IMODE(account.token_cache.stat().st_mode) == 0o600
    lock_path = account.token_cache.with_name(f"{account.token_cache.name}.lock")
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    cached = json.loads(account.token_cache.read_text(encoding="utf-8"))
    assert cached["refresh_token"] == "refresh-secret"
    assert not list(account.token_cache.parent.glob(f".{account.token_cache.name}.*"))


def test_expired_cache_refreshes_after_restart_without_leaking_tokens(
    tmp_path: Path,
) -> None:
    account = _outlook(tmp_path)
    assert account.token_cache is not None
    account.token_cache.parent.mkdir()
    account.token_cache.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": "expired-access-secret",
                "refresh_token": "old-refresh-secret",
                "expires_at": 10,
            }
        ),
        encoding="utf-8",
    )
    posted: list[dict[str, str]] = []

    def refresh(_url: str, values: dict[str, str]) -> dict[str, Any]:
        posted.append(dict(values))
        return {
            "access_token": "fresh-access-secret",
            "refresh_token": "fresh-refresh-secret",
            "expires_in": 3600,
        }

    assert OutlookOAuth(
        account, poster=refresh, clock=lambda: 100.0
    ).access_token() == ("fresh-access-secret")
    assert posted[0]["grant_type"] == "refresh_token"
    assert posted[0]["refresh_token"] == "old-refresh-secret"

    def unexpected(_url: str, _values: dict[str, str]) -> dict[str, Any]:
        raise AssertionError("the restarted process should use the refreshed cache")

    assert OutlookOAuth(
        account, poster=unexpected, clock=lambda: 101.0
    ).access_token() == ("fresh-access-secret")

    account.token_cache.write_text(
        json.dumps(
            {
                "version": 1,
                "access_token": "expired-access-secret",
                "refresh_token": "never-print-refresh-secret",
                "expires_at": 0,
            }
        ),
        encoding="utf-8",
    )

    def revoked(_url: str, _values: dict[str, str]) -> dict[str, Any]:
        return {
            "error": "invalid_grant",
            "error_description": "server echoed never-print-refresh-secret",
        }

    with pytest.raises(OutlookAuthorizationRequired) as raised:
        OutlookOAuth(account, poster=revoked, clock=lambda: 100.0).access_token()
    assert "never-print-refresh-secret" not in str(raised.value)


def test_xoauth2_authenticates_with_access_token_not_a_client_secret(
    tmp_path: Path,
) -> None:
    account = _outlook(tmp_path)

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def oauth2_login(self, address: str, token: str) -> None:
            self.calls.append((address, token))

    class OAuth:
        def access_token(self) -> str:
            return "short-lived-access"

    client = Client()
    authenticate_client(
        client,  # type: ignore[arg-type]
        account,
        oauth_factory=lambda _: OAuth(),  # type: ignore[arg-type]
    )

    assert client.calls == [(account.address, "short-lived-access")]


def test_cross_account_search_is_globally_ranked_and_reports_partial_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []

    @contextmanager
    def connector(account: MailAccountSettings):
        opened.append(account.key)
        if account.key == "outlook":
            raise RuntimeError("provider detail must not escape")
        yield object()

    class Reader:
        def __init__(self, _client: object, key: str, label: str) -> None:
            self.key = key
            self.label = label

        def search(self, query: str, **_kwargs: object) -> dict[str, Any]:
            return {
                "query": query,
                "searched_folders": 2,
                "results": [
                    {
                        "id": f"mail:{self.key}",
                        "account_key": self.key,
                        "account_label": self.label,
                        "relevance": 5,
                        "timestamp": "2026-09-01T10:00:00+00:00",
                    }
                ],
            }

    monkeypatch.setattr(reader_module, "MailReader", Reader)
    registry = MailAccountRegistry((_icloud(), _outlook(tmp_path)), connector=connector)

    result = MultiAccountMailReader(registry).search("project", limit=10)

    assert opened == ["icloud", "outlook"]
    assert result["partial"] is True
    assert result["failed_accounts"] == [
        {
            "key": "outlook",
            "label": "Personal Outlook",
            "error": "unavailable",
        }
    ]
    assert [item["account_key"] for item in result["results"]] == ["icloud"]
    assert "provider detail" not in json.dumps(result)


def test_account_filter_and_opaque_read_dispatch_open_only_source_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []

    @contextmanager
    def connector(account: MailAccountSettings):
        opened.append(account.key)
        yield object()

    class Reader:
        def __init__(self, _client: object, key: str, label: str) -> None:
            self.key = key
            self.label = label

        def search(self, query: str, **_kwargs: object) -> dict[str, Any]:
            score = 20 if self.key == "outlook" else 1
            return {
                "query": query,
                "searched_folders": 1,
                "results": [
                    {
                        "id": encode_mail_id(
                            MailReference("INBOX", 1, score, self.key)
                        ),
                        "account_key": self.key,
                        "account_label": self.label,
                        "relevance": score,
                        "timestamp": "2026-09-01T10:00:00+00:00",
                    }
                ],
            }

        def read(self, _value: str) -> dict[str, Any]:
            return {"account_key": self.key}

    monkeypatch.setattr(reader_module, "MailReader", Reader)
    registry = MailAccountRegistry((_icloud(), _outlook(tmp_path)), connector=connector)
    reader = MultiAccountMailReader(registry)

    combined = reader.search("project")
    assert [item["account_key"] for item in combined["results"]] == [
        "outlook",
        "icloud",
    ]
    assert opened == ["icloud", "outlook"]

    opened.clear()
    filtered = reader.search("project", account="outlook")
    assert [item["account_key"] for item in filtered["results"]] == ["outlook"]
    assert opened == ["outlook"]

    opened.clear()
    value = encode_mail_id(MailReference("INBOX", 1, 20, "outlook"))
    assert reader.read(value) == {"account_key": "outlook"}
    assert opened == ["outlook"]


def test_cli_account_filter_is_forwarded_without_affecting_default_search(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, Any]] = []

    class Mail:
        def search(self, query: str, **arguments: Any) -> dict[str, Any]:
            calls.append({"query": query, **arguments})
            return {"results": []}

    class Backend:
        @contextmanager
        def mail(self):
            yield Mail()

    main(
        ["mail", "search", "project", "--account", "outlook", "--limit", "4"],
        backend=Backend(),  # type: ignore[arg-type]
    )

    assert json.loads(capsys.readouterr().out) == {"results": []}
    assert calls == [
        {
            "query": "project",
            "since": None,
            "before": None,
            "limit": 4,
            "account": "outlook",
        }
    ]


def test_cli_device_authorization_can_be_completed_in_a_separate_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _config(
        tmp_path,
        f'''\
[outlook]
enabled = true
address = "owner@outlook.example"
client_id = "public-client-id"
token_cache = "{tmp_path / "token.json"}"
''',
    )
    authorized: list[str] = []

    class OAuth:
        def __init__(self, account: MailAccountSettings) -> None:
            self.account = account

        def authorize(self, notify: Any) -> None:
            authorized.append(self.account.key)
            notify(
                DeviceAuthorization(
                    "https://microsoft.example/device", "ABCD-EFGH", "", 900
                )
            )

    monkeypatch.setattr(cli_module, "OutlookOAuth", OAuth)

    main(
        ["--config", str(path), "mail", "authorize-outlook"],
        backend=ProductionBackend(path),
    )

    captured = capsys.readouterr()
    assert authorized == ["outlook"]
    assert "https://microsoft.example/device" in captured.err
    assert "ABCD-EFGH" in captured.err
    assert json.loads(captured.out)["status"] == "authorized"


def _create_legacy_state(path: Path) -> None:
    with sqlite3.connect(path) as database:
        database.executescript(
            """
            CREATE TABLE mail_jobs (
                job_id TEXT PRIMARY KEY,
                mailbox TEXT NOT NULL,
                uidvalidity INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                message_id TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                route_id TEXT,
                classification TEXT,
                importance TEXT,
                suggested_action TEXT,
                draft_reply TEXT,
                action TEXT,
                destination TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(mailbox, uidvalidity, uid)
            );
            CREATE TABLE mail_checkpoints (
                mailbox TEXT PRIMARY KEY,
                uidvalidity INTEGER NOT NULL,
                last_seen_uid INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO mail_jobs VALUES (
                'INBOX:9:4', 'INBOX', 9, 4, '<message>', 'done', 2,
                'route', 'travel', 'important', 'flag', 'draft', 'flag',
                NULL, NULL, 1.0, 2.0
            );
            INSERT INTO mail_checkpoints VALUES ('INBOX', 9, 4, 2.0);
            """
        )


def test_state_migration_preserves_legacy_jobs_and_qualifies_them_as_icloud(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mail.sqlite3"
    _create_legacy_state(path)

    state = MailState(path)
    state.initialize()

    job = state.get("INBOX:9:4")
    assert job is not None
    assert job.account_key == "icloud"
    assert job.status == "done"
    assert job.attempts == 2
    assert job.draft_reply == "draft"
    with sqlite3.connect(path) as database:
        database.row_factory = sqlite3.Row
        checkpoint = database.execute("SELECT * FROM mail_checkpoints").fetchone()
        assert checkpoint is not None
        assert checkpoint["account_key"] == "icloud"
        assert database.execute("PRAGMA user_version").fetchone()[0] == 2


def test_jobs_checkpoints_decisions_and_deduplication_are_account_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "mail.sqlite3"
    state = MailState(path)
    state.initialize()
    state.catch_up("INBOX", 10, [1], initial_baseline_uid=0, account_key="icloud")
    state.catch_up("INBOX", 10, [1], initial_baseline_uid=0, account_key="outlook")

    icloud_id = MailState.job_id("INBOX", 10, 1, "icloud")
    outlook_id = MailState.job_id("INBOX", 10, 1, "outlook")
    assert icloud_id != outlook_id
    assert state.get(icloud_id).account_key == "icloud"  # type: ignore[union-attr]
    assert state.get(outlook_id).account_key == "outlook"  # type: ignore[union-attr]

    state.start(icloud_id)
    monkeypatch.setenv("ARIADNE_MAIL_JOB_ID", icloud_id)
    monkeypatch.setenv("ARIADNE_MAIL_STATE", str(path))
    monkeypatch.setenv("ARIADNE_MAIL_ACCOUNT", "outlook")
    with pytest.raises(ValueError, match="not running"):
        from ariadne.mail import record_current_mail_decision

        record_current_mail_decision("travel", "important", "flag")

    monkeypatch.setenv("ARIADNE_MAIL_ACCOUNT", "icloud")
    record_current_mail_decision("travel", "important", "flag")
    state.finish(icloud_id)
    assert state.identify(outlook_id, "<same-delivery>") is False
    assert state.identify(icloud_id, "<same-delivery>") is False

    with sqlite3.connect(path) as database:
        count = database.execute("SELECT count(*) FROM mail_checkpoints").fetchone()[0]
    assert count == 2

    restarted = MailState(path)
    restarted.initialize()
    assert restarted.catch_up("INBOX", 10, [1, 2], account_key="outlook") == (2,)
    assert (
        restarted.get(MailState.job_id("INBOX", 10, 2, "outlook")).account_key
        == "outlook"
    )  # type: ignore[union-attr]


async def test_one_account_loop_failure_does_not_stop_another_provider(
    tmp_path: Path,
) -> None:
    failed = object.__new__(MailLoop)
    failed._stop = asyncio.Event()
    failed.account = _outlook(tmp_path)
    healthy = object.__new__(MailLoop)
    healthy._stop = asyncio.Event()
    healthy.account = _icloud()
    healthy_ran = asyncio.Event()

    async def fail_session() -> None:
        failed._stop.set()
        raise RuntimeError("outlook is offline")

    async def healthy_session() -> None:
        healthy_ran.set()
        healthy._stop.set()

    failed._session = fail_session  # type: ignore[method-assign]
    healthy._session = healthy_session  # type: ignore[method-assign]

    await asyncio.gather(failed.run_forever(), healthy.run_forever())

    assert healthy_ran.is_set()


async def test_current_event_flag_is_applied_only_in_its_source_account(
    tmp_path: Path,
) -> None:
    state = MailState(tmp_path / "mail.sqlite3")
    state.initialize()
    state.catch_up("INBOX", 10, [1], initial_baseline_uid=0, account_key="icloud")
    state.catch_up("INBOX", 10, [1], initial_baseline_uid=0, account_key="outlook")
    icloud_id = MailState.job_id("INBOX", 10, 1, "icloud")
    outlook_id = MailState.job_id("INBOX", 10, 1, "outlook")
    state.set_action(icloud_id, "flag", None)
    state.set_action(outlook_id, "flag", None)

    class Client:
        def __init__(self) -> None:
            self.flags: list[tuple[list[int], list[bytes]]] = []

        def fetch(
            self, uids: list[int], _query: list[bytes]
        ) -> dict[int, dict[bytes, bytes]]:
            return {uid: {b"BODY[]": b"Subject: harmless\r\n\r\n"} for uid in uids}

        def add_flags(self, uids: list[int], flags: list[bytes]) -> None:
            self.flags.append((uids, flags))

    routes = MailRoutes.model_validate(
        {
            "version": 1,
            "folders": {
                "newsletters": "Newsletters",
                "promotions": "Promotions",
                "receipts": "Receipts",
                "travel": "Travel",
                "notifications": "Notifications",
            },
            "rules": [],
        }
    )
    client = Client()
    processor = MailProcessor(
        cast(Any, client),
        routes,
        state,
        cast(Any, lambda _job_id: None),
        account_key="outlook",
        account_label="Personal Outlook",
    )
    processor.uidvalidity = 10

    await processor.process_available()

    assert client.flags == [([1], [b"\\Flagged"])]
    assert state.get(outlook_id).status == "done"  # type: ignore[union-attr]
    assert state.get(icloud_id).status == "pending"  # type: ignore[union-attr]
