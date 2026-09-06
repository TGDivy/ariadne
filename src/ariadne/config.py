"""Typed TOML configuration for Ariadne."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    DirectoryPath,
    Field,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)

from .codex.models import CodexTurnSettings, WebSearchSetting
from .profile import PROFILES, profile_for_attention
from .revisit import STATE_ENVIRONMENT as REVISIT_STATE_ENVIRONMENT
from .revisit import Attention
from .telegram.questions import (
    QUESTION_STATE_ENVIRONMENT,
    default_question_state_path,
)

DEFAULT_CONFIG_PATH = Path("~/.config/ariadne/config.toml")
CONFIG_PATH_ENVIRONMENT = "ARIADNE_CONFIG"


class ProfileOverrides(BaseModel):
    """Optional operator overrides for one declared turn profile."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    model: str | None = Field(default=None, min_length=1)
    effort: ReasoningEffort | None = None
    web_search: WebSearchSetting | None = None

    @field_validator("model", mode="before")
    @classmethod
    def strip_model(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TelegramIdentity(BaseModel):
    """Optional fields applied to the Telegram bot account on demand."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=512)
    short_description: str | None = Field(default=None, max_length=120)
    profile_photo: Path | None = None

    @field_validator("name", "description", "short_description", mode="before")
    @classmethod
    def empty_text_is_absent(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("profile_photo", mode="before")
    @classmethod
    def expand_profile_photo(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser() if value.strip() else None
        return value.expanduser() if isinstance(value, Path) else value


class TelegramConfig(BaseModel):
    """Telegram connection and presentation configuration."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    bot_token: SecretStr
    allowed_user_id: PositiveInt
    state: Path = Field(default_factory=default_question_state_path)
    identity: TelegramIdentity = Field(default_factory=TelegramIdentity)

    @field_validator("bot_token", mode="before")
    @classmethod
    def require_bot_token(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            if not value.get_secret_value().strip():
                raise ValueError("Telegram bot token must not be empty.")
            return value
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Telegram bot token must not be empty.")
        return value

    @field_validator("state", mode="before")
    @classmethod
    def expand_state_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value.expanduser() if isinstance(value, Path) else value


class ICloudConfig(BaseModel):
    """Shared credentials for opt-in iCloud services."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    username: str | None = None
    app_password: SecretStr | None = None

    @field_validator("username", mode="before")
    @classmethod
    def empty_username_is_absent(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("app_password", mode="before")
    @classmethod
    def empty_password_is_absent(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def require_complete_credentials(self) -> ICloudConfig:
        if (self.username is None) != (self.app_password is None):
            raise ValueError("iCloud credentials require username and app_password.")
        return self


class MailConfig(BaseModel):
    """Opt-in iCloud Mail configuration, including legacy credentials."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    username: str | None = None
    app_password: SecretStr | None = None
    routes: Path | None = None
    state: Path = Field(
        default_factory=lambda: Path("~/.local/state/ariadne/mail.sqlite3").expanduser()
    )

    @field_validator("username", mode="before")
    @classmethod
    def empty_username_is_absent(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("app_password", mode="before")
    @classmethod
    def empty_password_is_absent(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("routes", "state", mode="before")
    @classmethod
    def expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser() if value.strip() else None
        return value.expanduser() if isinstance(value, Path) else value

    @model_validator(mode="after")
    def require_complete_enabled_mail(self) -> MailConfig:
        if (self.username is None) != (self.app_password is None):
            raise ValueError(
                "Legacy mail credentials require username and app_password."
            )
        if self.enabled and self.routes is None:
            raise ValueError("Enabled mail requires routes.")
        if self.enabled and self.routes is not None and not self.routes.is_file():
            raise ValueError("Mail routes must point to a YAML file.")
        return self


class CalendarConfig(BaseModel):
    """Opt-in, on-demand iCloud Calendar configuration."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    timezone: str = "UTC"
    default_calendar: str | None = None

    @field_validator("timezone", mode="before")
    @classmethod
    def strip_timezone(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def require_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(
                "Calendar timezone must be a valid IANA timezone."
            ) from error
        return value

    @field_validator("default_calendar", mode="before")
    @classmethod
    def empty_default_is_absent(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


class HealthConfig(BaseModel):
    """Opt-in access to the owner's read-only Ithaca health boundary."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    api_url: AnyHttpUrl | None = None
    read_token: SecretStr | None = None
    timezone: str = "UTC"
    timeout_seconds: int = Field(default=30, ge=1, le=120)

    @field_validator("api_url")
    @classmethod
    def require_safe_api_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is None:
            return None
        if value.username or value.password or value.query or value.fragment:
            raise ValueError(
                "Health api_url must not contain credentials, a query, or a fragment."
            )
        host = value.host.strip("[]") if value.host is not None else None
        if value.scheme != "https" and host not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("Health api_url must use HTTPS except on localhost.")
        return value

    @field_validator("read_token", mode="before")
    @classmethod
    def normalize_read_token(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            token = value.get_secret_value()
        elif isinstance(value, str):
            token = value
        else:
            return value
        if token != token.strip():
            raise ValueError("Health read_token must not contain surrounding space.")
        if token and (
            len(token) < 32
            or any(not 0x21 <= ord(character) <= 0x7E for character in token)
        ):
            raise ValueError(
                "Health read_token must contain at least 32 printable ASCII "
                "characters without whitespace."
            )
        return token or None

    @field_validator("timezone", mode="before")
    @classmethod
    def strip_timezone(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def require_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError(
                "Health timezone must be a valid IANA timezone."
            ) from error
        return value

    @model_validator(mode="after")
    def require_complete_enabled_health(self) -> HealthConfig:
        if self.enabled and (self.api_url is None or self.read_token is None):
            raise ValueError("Enabled health access requires api_url and read_token.")
        return self


class RevisitConfig(BaseModel):
    """Always-on local settings for one-off future revisits."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    state: Path = Field(
        default_factory=lambda: Path(
            "~/.local/state/ariadne/revisits.sqlite3"
        ).expanduser()
    )
    poll_interval_seconds: PositiveInt = 15

    @field_validator("state", mode="before")
    @classmethod
    def expand_state_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value.expanduser() if isinstance(value, Path) else value


class TelemetryConfig(BaseModel):
    """Opt-in OTLP/HTTP export configuration."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    endpoint: AnyHttpUrl | None = None
    authorization: SecretStr | None = None
    service_name: str = Field(default="ariadne", min_length=1)
    metrics: bool = True
    traces: bool = True
    export_interval_seconds: PositiveInt = 60

    @field_validator("authorization", mode="before")
    @classmethod
    def empty_authorization_is_absent(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("service_name", mode="before")
    @classmethod
    def strip_service_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def require_complete_enabled_telemetry(self) -> TelemetryConfig:
        if not self.enabled:
            return self
        missing = []
        if self.endpoint is None:
            missing.append("endpoint")
        if self.authorization is None:
            missing.append("authorization")
        if missing:
            raise ValueError("Enabled telemetry requires: " + ", ".join(missing) + ".")
        if not self.metrics and not self.traces:
            raise ValueError("Enabled telemetry requires metrics or traces.")
        return self


class Settings(BaseModel):
    """Complete validated Ariadne configuration."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: Literal[1]
    human_name: str = Field(min_length=1)
    vault: DirectoryPath
    personality: Path | None = None
    telegram: TelegramConfig
    icloud: ICloudConfig = Field(default_factory=ICloudConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    revisits: RevisitConfig = Field(default_factory=RevisitConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    profiles: dict[str, ProfileOverrides] = Field(default_factory=dict)

    @field_validator("human_name", mode="before")
    @classmethod
    def require_human_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("vault", mode="before")
    @classmethod
    def expand_vault(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Vault path must not be empty.")
            return Path(value).expanduser()
        return value

    @field_validator("vault")
    @classmethod
    def validate_vault(cls, value: Path) -> Path:
        vault = value.resolve()
        if not (vault / ".git").exists():
            raise ValueError("Vault must point to a Git repository.")
        return vault

    @field_validator("personality", mode="before")
    @classmethod
    def expand_personality(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser() if value.strip() else None
        return value.expanduser() if isinstance(value, Path) else value

    @field_validator("personality")
    @classmethod
    def validate_personality(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_file():
            raise ValueError("Personality must point to a readable Markdown file.")
        return value

    @model_validator(mode="after")
    def reject_unknown_profiles(self) -> Settings:
        unknown = self.profiles.keys() - PROFILES.keys()
        if unknown:
            raise ValueError("Unknown turn profiles: " + ", ".join(sorted(unknown)))
        if (
            self.mail.enabled or self.calendar.enabled
        ) and self.icloud_credentials is None:
            raise ValueError(
                "Enabled iCloud services require [icloud] username and app_password."
            )
        return self

    @property
    def icloud_credentials(self) -> tuple[str, SecretStr] | None:
        if self.icloud.username is not None and self.icloud.app_password is not None:
            return self.icloud.username, self.icloud.app_password
        if self.mail.username is not None and self.mail.app_password is not None:
            return self.mail.username, self.mail.app_password
        return None

    @property
    def telegram_bot_token(self) -> str:
        return self.telegram.bot_token.get_secret_value()

    @property
    def allowed_user_id(self) -> int:
        return self.telegram.allowed_user_id

    @property
    def health_network_domains(self) -> tuple[str, ...]:
        if not self.health.enabled or self.health.api_url is None:
            return ()
        host = self.health.api_url.host
        return (host.strip("[]"),) if host is not None else ()

    def turn_settings(self, name: str) -> CodexTurnSettings:
        profile = PROFILES[name]
        override = self.profiles.get(name, ProfileOverrides())
        return CodexTurnSettings(
            model=override.model or profile.model,
            effort=override.effort or profile.effort,
            web_search=override.web_search or profile.web_search,
        )

    @property
    def codex_turn_settings(self) -> CodexTurnSettings:
        return self.turn_settings("telegram")

    @property
    def mail_turn_settings(self) -> CodexTurnSettings:
        return self.turn_settings("mail")

    def revisit_turn_settings(self, attention: Attention) -> CodexTurnSettings:
        """Resolve the operator-configurable profile for one explicit level."""
        return self.turn_settings(profile_for_attention(attention).name)

    @property
    def mcp_environment(self) -> dict[str, str]:
        return {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_ALLOWED_USER_ID": str(self.allowed_user_id),
            QUESTION_STATE_ENVIRONMENT: str(self.telegram.state.resolve()),
            REVISIT_STATE_ENVIRONMENT: str(self.revisits.state.resolve()),
        }

    @property
    def mail_settings(self) -> MailSettings | None:
        if not self.mail.enabled:
            return None
        assert self.icloud_credentials is not None
        username, app_password = self.icloud_credentials
        assert self.mail.routes is not None
        return MailSettings(
            username=username,
            app_password=app_password,
            routes=self.mail.routes.resolve(),
            state=self.mail.state.resolve(),
        )

    @property
    def revisit_settings(self) -> RevisitSettings:
        return RevisitSettings(
            state=self.revisits.state.resolve(),
            poll_interval_seconds=self.revisits.poll_interval_seconds,
        )


@dataclass(frozen=True, slots=True)
class MailSettings:
    """The complete enabled iCloud Mail runtime configuration."""

    username: str
    app_password: SecretStr
    routes: Path
    state: Path


@dataclass(frozen=True, slots=True)
class RevisitSettings:
    """The complete always-on revisit runtime configuration."""

    state: Path
    poll_interval_seconds: int


def config_path(
    path: Path | str | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
) -> Path:
    """Return the explicit, environment-selected, or default TOML path."""
    selected = path or environ.get(CONFIG_PATH_ENVIRONMENT) or DEFAULT_CONFIG_PATH
    return Path(selected).expanduser().resolve()


def load_settings(
    path: Path | str | None = None,
    *,
    environ: Mapping[str, str] = os.environ,
) -> Settings:
    """Load Ariadne's TOML configuration."""
    selected = config_path(path, environ=environ)
    if not selected.is_file():
        raise ValueError(f"Ariadne config does not exist: {selected}")
    try:
        data = tomllib.loads(selected.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Invalid Ariadne config at {selected}: {error}") from error
    return Settings.model_validate(data)


def settings_payload(settings: Settings) -> dict[str, Any]:
    """Return effective configuration with secrets replaced, never serialized."""
    identity = settings.telegram.identity
    return {
        "version": settings.version,
        "human_name": settings.human_name,
        "vault": str(settings.vault),
        "personality": str(settings.personality) if settings.personality else None,
        "telegram": {
            "bot_token": "<redacted>",
            "allowed_user_id": settings.allowed_user_id,
            "state": str(settings.telegram.state),
            "identity": {
                "name": identity.name,
                "description": identity.description,
                "short_description": identity.short_description,
                "profile_photo": (
                    str(identity.profile_photo) if identity.profile_photo else None
                ),
            },
        },
        "icloud": {
            "username": settings.icloud.username,
            "app_password": (
                "<redacted>" if settings.icloud.app_password is not None else None
            ),
        },
        "mail": {
            "enabled": settings.mail.enabled,
            "username": settings.mail.username,
            "app_password": (
                "<redacted>" if settings.mail.app_password is not None else None
            ),
            "routes": str(settings.mail.routes) if settings.mail.routes else None,
            "state": str(settings.mail.state),
        },
        "calendar": {
            "enabled": settings.calendar.enabled,
            "timezone": settings.calendar.timezone,
            "default_calendar": settings.calendar.default_calendar,
        },
        "health": {
            "enabled": settings.health.enabled,
            "api_url": (
                str(settings.health.api_url)
                if settings.health.api_url is not None
                else None
            ),
            "read_token": (
                "<redacted>" if settings.health.read_token is not None else None
            ),
            "timezone": settings.health.timezone,
            "timeout_seconds": settings.health.timeout_seconds,
        },
        "revisits": {
            "state": str(settings.revisits.state),
            "poll_interval_seconds": settings.revisits.poll_interval_seconds,
        },
        "telemetry": {
            "enabled": settings.telemetry.enabled,
            "endpoint": (
                str(settings.telemetry.endpoint)
                if settings.telemetry.endpoint is not None
                else None
            ),
            "authorization": (
                "<redacted>" if settings.telemetry.authorization is not None else None
            ),
            "service_name": settings.telemetry.service_name,
            "metrics": settings.telemetry.metrics,
            "traces": settings.telemetry.traces,
            "export_interval_seconds": settings.telemetry.export_interval_seconds,
        },
        "profiles": {
            name: {
                "model": settings.turn_settings(name).model,
                "effort": settings.turn_settings(name).effort.value,
                "web_search": settings.turn_settings(name).web_search,
            }
            for name in PROFILES
        },
    }
