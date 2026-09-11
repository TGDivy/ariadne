"""Provider-neutral mailbox connections and Outlook.com OAuth authorization."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from imapclient import IMAPClient  # type: ignore[import-untyped]
from imapclient.exceptions import (  # type: ignore[import-untyped]
    IMAPClientError,
    LoginError,
)

from ..config import MailAccountSettings

OUTLOOK_AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
OUTLOOK_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_SCOPES = f"{OUTLOOK_IMAP_SCOPE} offline_access"
TOKEN_REFRESH_MARGIN_SECONDS = 120


class MailAccountError(RuntimeError):
    """A safe account-qualified error that never includes provider responses."""

    def __init__(self, account_key: str, message: str) -> None:
        super().__init__(message)
        self.account_key = account_key


class MailAuthenticationError(MailAccountError):
    """The configured account could not authenticate."""


class MailUnavailableError(MailAccountError):
    """The configured account could not complete an IMAP operation."""


class OutlookAuthorizationRequired(MailAuthenticationError):
    """The owner must run the explicit device-authorization command."""


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    verification_uri: str
    user_code: str
    message: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class _Token:
    access_token: str
    refresh_token: str
    expires_at: float


class FormPoster(Protocol):
    def __call__(self, url: str, values: Mapping[str, str]) -> dict[str, Any]: ...


def _post_form(url: str, values: Mapping[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=30)  # noqa: S310
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(
            "Microsoft authorization is temporarily unavailable."
        ) from error
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, OSError) as error:
        raise RuntimeError(
            "Microsoft authorization returned an invalid response."
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError("Microsoft authorization returned an invalid response.")
    return payload


class OutlookTokenCache:
    """A minimal refreshable token cache with atomic owner-only updates."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize refresh-token rotation across service and CLI processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self) -> _Token | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "The Outlook token cache is unreadable; authorize Outlook again."
            ) from error
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise RuntimeError(
                "The Outlook token cache is invalid; authorize Outlook again."
            )
        access = raw.get("access_token")
        refresh = raw.get("refresh_token")
        expires_at = raw.get("expires_at")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or not isinstance(expires_at, (int, float))
            or isinstance(expires_at, bool)
        ):
            raise RuntimeError(
                "The Outlook token cache is invalid; authorize Outlook again."
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError as error:
            raise RuntimeError(
                "The Outlook token cache permissions could not be secured."
            ) from error
        return _Token(access, refresh, float(expires_at))

    def store(self, token: _Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            # Existing shared state parents may intentionally have a broader mode.
            pass
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", text=True
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "version": 1,
                        "access_token": token.access_token,
                        "refresh_token": token.refresh_token,
                        "expires_at": token.expires_at,
                    },
                    stream,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


class OutlookOAuth:
    """Microsoft consumer device authorization and refresh-token exchange."""

    def __init__(
        self,
        account: MailAccountSettings,
        *,
        poster: FormPoster = _post_form,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if account.provider != "outlook" or account.client_id is None:
            raise ValueError("Outlook OAuth requires an Outlook account and client_id.")
        if account.token_cache is None:
            raise ValueError("Outlook OAuth requires a private token_cache path.")
        self.account = account
        self.poster = poster
        self.clock = clock
        self.sleep = sleep
        self.cache = OutlookTokenCache(account.token_cache)

    def access_token(self) -> str:
        with self.cache.locked():
            token = self.cache.load()
            if token is None:
                raise OutlookAuthorizationRequired(
                    self.account.key,
                    "Outlook is not authorized. Run `ariadne mail authorize-outlook`.",
                )
            if token.expires_at - self.clock() > TOKEN_REFRESH_MARGIN_SECONDS:
                return token.access_token
            refreshed = self._token_request(
                {
                    "client_id": self.account.client_id or "",
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                    "scope": OUTLOOK_SCOPES,
                },
                previous_refresh_token=token.refresh_token,
            )
            self.cache.store(refreshed)
            return refreshed.access_token

    def authorize(
        self, notify: Callable[[DeviceAuthorization], None]
    ) -> DeviceAuthorization:
        payload = self.poster(
            f"{OUTLOOK_AUTHORITY}/devicecode",
            {
                "client_id": self.account.client_id or "",
                "scope": OUTLOOK_SCOPES,
            },
        )
        device_code = payload.get("device_code")
        user_code = payload.get("user_code")
        verification_uri = payload.get("verification_uri")
        expires_in = payload.get("expires_in")
        interval = payload.get("interval", 5)
        if (
            not isinstance(device_code, str)
            or not device_code
            or not isinstance(user_code, str)
            or not user_code
            or not isinstance(verification_uri, str)
            or not verification_uri
            or not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or expires_in <= 0
            or not isinstance(interval, int)
            or isinstance(interval, bool)
            or interval <= 0
        ):
            raise RuntimeError("Microsoft device authorization could not be started.")
        details = DeviceAuthorization(
            verification_uri=verification_uri,
            user_code=user_code,
            message=str(payload.get("message") or ""),
            expires_in=expires_in,
        )
        notify(details)
        deadline = self.clock() + expires_in
        poll_interval = interval
        while self.clock() < deadline:
            self.sleep(poll_interval)
            payload = self.poster(
                f"{OUTLOOK_AUTHORITY}/token",
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.account.client_id or "",
                    "device_code": device_code,
                },
            )
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                poll_interval += 5
                continue
            if error in {"authorization_declined", "access_denied"}:
                raise RuntimeError("Outlook authorization was declined.")
            if error in {"expired_token", "bad_verification_code"}:
                raise RuntimeError("The Outlook authorization code expired.")
            token = self._parse_token(payload)
            with self.cache.locked():
                self.cache.store(token)
            return details
        raise RuntimeError("The Outlook authorization code expired.")

    def _token_request(
        self,
        values: Mapping[str, str],
        *,
        previous_refresh_token: str,
    ) -> _Token:
        payload = self.poster(f"{OUTLOOK_AUTHORITY}/token", values)
        if payload.get("error") is not None:
            raise OutlookAuthorizationRequired(
                self.account.key,
                "Outlook authorization has expired or was revoked. Run "
                "`ariadne mail authorize-outlook` again.",
            )
        return self._parse_token(payload, previous_refresh_token)

    def _parse_token(
        self, payload: Mapping[str, Any], previous_refresh_token: str = ""
    ) -> _Token:
        access = payload.get("access_token")
        refresh = payload.get("refresh_token", previous_refresh_token)
        expires_in = payload.get("expires_in")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or not math.isfinite(expires_in)
            or expires_in <= 0
        ):
            raise RuntimeError("Microsoft authorization returned an invalid token.")
        return _Token(access, refresh, self.clock() + float(expires_in))


def authenticate_client(
    client: IMAPClient,
    account: MailAccountSettings,
    *,
    oauth_factory: Callable[[MailAccountSettings], OutlookOAuth] = OutlookOAuth,
) -> None:
    """Authenticate one connected IMAP client without exposing auth material."""
    try:
        if account.provider == "icloud":
            if account.password is None:
                raise ValueError("The iCloud account has no app password.")
            client.login(account.address, account.password.get_secret_value())
        else:
            token = oauth_factory(account).access_token()
            client.oauth2_login(account.address, token)
    except OutlookAuthorizationRequired:
        raise
    except (IMAPClientError, LoginError, ValueError, RuntimeError) as error:
        raise MailAuthenticationError(
            account.key, f"{account.label} rejected the configured credentials."
        ) from error


ClientConstructor = Callable[..., IMAPClient]


@contextmanager
def connect_account(
    account: MailAccountSettings,
    *,
    timeout: int | None = 30,
    client_factory: ClientConstructor = IMAPClient,
    oauth_factory: Callable[[MailAccountSettings], OutlookOAuth] = OutlookOAuth,
) -> Iterator[IMAPClient]:
    """Open and authenticate one TLS IMAP connection."""
    try:
        keywords: dict[str, object] = {"port": 993, "ssl": True}
        if timeout is not None:
            keywords["timeout"] = timeout
        client = client_factory(account.host, **keywords)
    except Exception as error:
        raise MailUnavailableError(
            account.key, f"{account.label} Mail is currently unavailable."
        ) from error
    try:
        authenticate_client(client, account, oauth_factory=oauth_factory)
        yield client
    finally:
        try:
            client.logout()
        except Exception:
            pass


Connector = Callable[[MailAccountSettings], AbstractContextManager[IMAPClient]]


class MailAccountRegistry:
    """Stable account lookup and independently opened mailbox connections."""

    def __init__(
        self,
        accounts: Sequence[MailAccountSettings],
        *,
        connector: Connector | None = None,
    ) -> None:
        self._accounts = {account.key: account for account in accounts}
        if len(self._accounts) != len(accounts):
            raise ValueError("Mail account keys must be unique.")
        self._connector = connector

    @property
    def accounts(self) -> tuple[MailAccountSettings, ...]:
        return tuple(self._accounts.values())

    def get(self, key: str) -> MailAccountSettings:
        try:
            return self._accounts[key]
        except KeyError as error:
            raise ValueError(
                f"Mail account {key!r} is not enabled in Ariadne's configuration."
            ) from error

    @contextmanager
    def connect(self, key: str) -> Iterator[IMAPClient]:
        account = self.get(key)
        manager = (
            self._connector(account)
            if self._connector is not None
            else connect_account(account)
        )
        with manager as client:
            yield client


def select_account(
    accounts: Sequence[MailAccountSettings],
    key: str | None,
    *,
    require_explicit_when_multiple: bool = False,
) -> MailAccountSettings:
    """Resolve an operator-selected account without an unsafe implicit mutation."""
    if key is None:
        if not accounts:
            raise ValueError("No Mail accounts are enabled.")
        if len(accounts) > 1 and require_explicit_when_multiple:
            raise ValueError("Multiple Mail accounts are enabled; pass --account.")
        return accounts[0]
    matches = [account for account in accounts if account.key == key]
    if not matches:
        available = ", ".join(account.key for account in accounts) or "none"
        raise ValueError(f"Unknown Mail account {key!r}; available: {available}.")
    return matches[0]
