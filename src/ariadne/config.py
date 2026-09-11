"""Typed TOML configuration for Ariadne."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time as daytime
from decimal import Decimal, InvalidOperation
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

from .browser.capability import SOCKET_ENVIRONMENT as BROWSER_SOCKET_ENVIRONMENT
from .codex.models import CodexTurnSettings, WebSearchSetting
from .grocery.capability import (
    APPROVAL_TTL_ENVIRONMENT as GROCERY_APPROVAL_TTL_ENVIRONMENT,
)
from .grocery.capability import (
    BASE_URL_ENVIRONMENT as GROCERY_BASE_URL_ENVIRONMENT,
)
from .grocery.capability import (
    CHECKOUT_ENVIRONMENT as GROCERY_CHECKOUT_ENVIRONMENT,
)
from .grocery.capability import (
    PROFILE_ENVIRONMENT as GROCERY_PROFILE_ENVIRONMENT,
)
from .grocery.capability import (
    STATE_ENVIRONMENT as GROCERY_STATE_ENVIRONMENT,
)
from .grocery.capability import (
    TOLERANCE_ENVIRONMENT as GROCERY_TOLERANCE_ENVIRONMENT,
)
from .profile import PROFILES, profile_for_attention
from .revisit import STATE_ENVIRONMENT as REVISIT_STATE_ENVIRONMENT
from .revisit import Attention
from .stewardship import STATE_ENVIRONMENT as STEWARDSHIP_STATE_ENVIRONMENT
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
    voice_transcription_command: tuple[str, ...] | None = None
    voice_transcription_timeout_seconds: int = Field(default=120, ge=1, le=600)

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

    @field_validator("voice_transcription_command", mode="before")
    @classmethod
    def normalize_voice_command(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            raise ValueError("voice_transcription_command must be an argument list.")
        command = tuple(str(argument).strip() for argument in value)
        if not command or any(not argument for argument in command):
            raise ValueError("voice_transcription_command cannot contain empty values.")
        if sum(argument.count("{input}") for argument in command) != 1:
            raise ValueError(
                "voice_transcription_command needs exactly one {input} placeholder."
            )
        return command


class ICloudConfig(BaseModel):
    """Shared credentials for opt-in iCloud services."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    username: str | None = None
    app_password: SecretStr | None = None
    mail_label: str = Field(default="iCloud", min_length=1, max_length=80)

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

    @field_validator("mail_label", mode="before")
    @classmethod
    def strip_mail_label(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class OutlookConfig(BaseModel):
    """Personal Outlook.com OAuth settings for opt-in Mail access."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    address: str | None = None
    client_id: str | None = None
    token_cache: Path = Field(
        default_factory=lambda: Path(
            "~/.local/state/ariadne/outlook-token.json"
        ).expanduser()
    )
    label: str = Field(default="Outlook", min_length=1, max_length=80)

    @field_validator("address", "client_id", mode="before")
    @classmethod
    def empty_text_is_absent(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("label", mode="before")
    @classmethod
    def strip_label(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("token_cache", mode="before")
    @classmethod
    def expand_token_cache(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("Outlook token_cache must not be empty.")
            return Path(value).expanduser()
        return value.expanduser() if isinstance(value, Path) else value

    @model_validator(mode="after")
    def require_complete_enabled_outlook(self) -> OutlookConfig:
        if self.enabled and (self.address is None or self.client_id is None):
            raise ValueError("Enabled Outlook Mail requires address and client_id.")
        return self


class MailConfig(BaseModel):
    """Opt-in provider-neutral Mail configuration, including legacy credentials."""

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


class BrowserConfig(BaseModel):
    """Opt-in paths and limits for the private Chromium daemon."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    state: Path = Field(
        default_factory=lambda: Path(
            "~/.local/state/ariadne/browser/browser.sqlite3"
        ).expanduser()
    )
    profiles: Path = Field(
        default_factory=lambda: Path(
            "~/.local/share/ariadne/browser/profiles"
        ).expanduser()
    )
    artifacts: Path = Field(
        default_factory=lambda: Path(
            "~/.local/state/ariadne/browser/artifacts"
        ).expanduser()
    )
    socket: Path = Field(
        default_factory=lambda: Path(
            "~/.local/state/ariadne/browser/browser.sock"
        ).expanduser()
    )
    headless: bool = False
    executable_path: Path | None = None
    takeover_url: AnyHttpUrl | None = None
    lease_seconds: int = Field(default=900, ge=30, le=86_400)
    action_timeout_seconds: int = Field(default=30, ge=1, le=300)
    observation_max_chars: int = Field(default=12_000, ge=1_000, le=50_000)
    observation_max_elements: int = Field(default=120, ge=10, le=500)
    journal_retention_days: int = Field(default=14, ge=1, le=365)
    journal_max_entries: int = Field(default=5_000, ge=100, le=100_000)

    @field_validator("state", "profiles", "artifacts", "socket", mode="before")
    @classmethod
    def expand_browser_path(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Browser private paths must not be empty.")
            return Path(value).expanduser()
        return value.expanduser() if isinstance(value, Path) else value

    @field_validator("executable_path", mode="before")
    @classmethod
    def expand_browser_executable(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser() if value.strip() else None
        return value.expanduser() if isinstance(value, Path) else value

    @field_validator("takeover_url")
    @classmethod
    def require_private_takeover_url(
        cls, value: AnyHttpUrl | None
    ) -> AnyHttpUrl | None:
        if value is None:
            return None
        if value.username or value.password or value.query or value.fragment:
            raise ValueError(
                "Browser takeover_url must not contain credentials, query, or fragment."
            )
        host = value.host.strip("[]") if value.host is not None else None
        if value.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Browser takeover_url must use HTTPS except on localhost.")
        return value

    @model_validator(mode="after")
    def require_safe_browser_layout(self) -> BrowserConfig:
        resolved = {
            self.state.resolve(),
            self.profiles.resolve(),
            self.artifacts.resolve(),
            self.socket.resolve(),
        }
        if len(resolved) != 4:
            raise ValueError(
                "Browser state, profile, artifact, and socket paths must differ."
            )
        if self.enabled and self.executable_path is not None:
            executable = self.executable_path.resolve()
            if not executable.is_file():
                raise ValueError("Browser executable_path must point to a file.")
        return self


class GroceryConfig(BaseModel):
    """Opt-in Waitrose ordering above the private browser capability."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    state: Path = Field(
        default_factory=lambda: Path(
            "~/.local/state/ariadne/grocery.sqlite3"
        ).expanduser()
    )
    browser_profile: str = Field(default="personal", min_length=1, max_length=64)
    base_url: AnyHttpUrl = Field(default=AnyHttpUrl("https://www.waitrose.com"))
    # Real checkout stays off until the documented owner-observed dry runs pass.
    checkout_enabled: bool = False
    weighted_tolerance: Decimal = Field(
        default=Decimal("1.00"), ge=Decimal("0"), le=Decimal("20")
    )
    approval_ttl_seconds: int = Field(default=900, ge=30, le=3600)

    @field_validator("state", mode="before")
    @classmethod
    def expand_state_path(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Grocery state path must not be empty.")
            return Path(value).expanduser()
        return value.expanduser() if isinstance(value, Path) else value

    @field_validator("browser_profile", mode="before")
    @classmethod
    def strip_browser_profile(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("browser_profile")
    @classmethod
    def require_simple_profile_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError("Grocery browser_profile must be a simple identifier.")
        return value

    @field_validator("weighted_tolerance", mode="before")
    @classmethod
    def read_tolerance(cls, value: object) -> object:
        if isinstance(value, str | int | float):
            try:
                return Decimal(str(value)).quantize(Decimal("0.01"))
            except InvalidOperation as error:
                raise ValueError(
                    "Grocery weighted_tolerance must be a decimal amount."
                ) from error
        return value

    @field_validator("base_url")
    @classmethod
    def require_private_base_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError(
                "Grocery base_url must not contain credentials, query, or fragment."
            )
        host = value.host.strip("[]") if value.host is not None else None
        if value.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(
                "Grocery base_url must use HTTPS except for a local fixture site."
            )
        return value


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


class StewardshipConfig(BaseModel):
    """Opt-in daily creative cycle and its local waking window."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    timezone: str = "UTC"
    waking_start: daytime = daytime(9, 0)
    waking_end: daytime = daytime(21, 0)
    state: Path = Field(
        default_factory=lambda: Path(
            "~/.local/state/ariadne/stewardship.sqlite3"
        ).expanduser()
    )
    poll_interval_seconds: PositiveInt = 60

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
                "Stewardship timezone must be a valid IANA timezone."
            ) from error
        return value

    @field_validator("waking_start", "waking_end")
    @classmethod
    def require_minute_precision(cls, value: daytime) -> daytime:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError(
                "Stewardship waking times use local HH:MM values without a timezone."
            )
        return value

    @field_validator("state", mode="before")
    @classmethod
    def expand_state_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(value).expanduser()
        return value.expanduser() if isinstance(value, Path) else value

    @model_validator(mode="after")
    def require_ordered_waking_window(self) -> StewardshipConfig:
        if self.waking_end <= self.waking_start:
            raise ValueError("Stewardship waking_end must be later than waking_start.")
        return self


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
    workspace: DirectoryPath | None = None
    personality: Path | None = None
    telegram: TelegramConfig
    icloud: ICloudConfig = Field(default_factory=ICloudConfig)
    outlook: OutlookConfig = Field(default_factory=OutlookConfig)
    mail: MailConfig = Field(default_factory=MailConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    grocery: GroceryConfig = Field(default_factory=GroceryConfig)
    revisits: RevisitConfig = Field(default_factory=RevisitConfig)
    stewardship: StewardshipConfig = Field(default_factory=StewardshipConfig)
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

    @field_validator("workspace", mode="before")
    @classmethod
    def expand_workspace(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Workspace path must not be empty.")
            return Path(value).expanduser()
        return value

    @property
    def agent_workspace(self) -> Path:
        """Return the launch directory, preserving the historical vault default."""
        return self.workspace.resolve() if self.workspace is not None else self.vault

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
        if self.calendar.enabled and self.icloud_credentials is None:
            raise ValueError(
                "Enabled iCloud services require [icloud] username and app_password."
            )
        if (
            self.mail.enabled
            and self.icloud_credentials is None
            and not self.outlook.enabled
        ):
            raise ValueError(
                "Enabled Mail requires iCloud credentials or an enabled Outlook "
                "account."
            )
        if self.grocery.enabled and not self.browser.enabled:
            raise ValueError(
                "Enabled grocery ordering requires the [browser] capability."
            )
        if self.grocery.checkout_enabled and not self.grocery.enabled:
            raise ValueError("Grocery checkout_enabled requires [grocery].enabled.")
        if self.grocery.state.resolve() == self.browser.state.resolve():
            raise ValueError("Grocery and browser state paths must differ.")
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

    @property
    def stewardship_turn_settings(self) -> CodexTurnSettings:
        return self.turn_settings("stewardship")

    def revisit_turn_settings(self, attention: Attention) -> CodexTurnSettings:
        """Resolve the operator-configurable profile for one explicit level."""
        return self.turn_settings(profile_for_attention(attention).name)

    @property
    def mcp_environment(self) -> dict[str, str]:
        environment = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_ALLOWED_USER_ID": str(self.allowed_user_id),
            QUESTION_STATE_ENVIRONMENT: str(self.telegram.state.resolve()),
            REVISIT_STATE_ENVIRONMENT: str(self.revisits.state.resolve()),
            STEWARDSHIP_STATE_ENVIRONMENT: str(self.stewardship.state.resolve()),
        }
        if self.browser.enabled:
            environment[BROWSER_SOCKET_ENVIRONMENT] = str(self.browser.socket.resolve())
        if self.grocery.enabled:
            environment.update(
                {
                    GROCERY_STATE_ENVIRONMENT: str(self.grocery.state.resolve()),
                    GROCERY_PROFILE_ENVIRONMENT: self.grocery.browser_profile,
                    GROCERY_BASE_URL_ENVIRONMENT: str(self.grocery.base_url),
                    GROCERY_CHECKOUT_ENVIRONMENT: (
                        "1" if self.grocery.checkout_enabled else "0"
                    ),
                    GROCERY_TOLERANCE_ENVIRONMENT: str(self.grocery.weighted_tolerance),
                    GROCERY_APPROVAL_TTL_ENVIRONMENT: str(
                        self.grocery.approval_ttl_seconds
                    ),
                }
            )
        return environment

    @property
    def mail_accounts(self) -> tuple[MailAccountSettings, ...]:
        """Return every fully configured mailbox in stable routing order."""
        accounts: list[MailAccountSettings] = []
        if self.icloud_credentials is not None:
            username, app_password = self.icloud_credentials
            accounts.append(
                MailAccountSettings(
                    key="icloud",
                    label=self.icloud.mail_label,
                    provider="icloud",
                    address=username,
                    host="imap.mail.me.com",
                    password=app_password,
                )
            )
        if self.outlook.enabled:
            assert self.outlook.address is not None
            assert self.outlook.client_id is not None
            accounts.append(
                MailAccountSettings(
                    key="outlook",
                    label=self.outlook.label,
                    provider="outlook",
                    address=self.outlook.address,
                    host="outlook.office365.com",
                    client_id=self.outlook.client_id,
                    token_cache=self.outlook.token_cache.resolve(),
                )
            )
        return tuple(accounts)

    @property
    def mail_settings(self) -> MailSettings | None:
        if not self.mail.enabled:
            return None
        assert self.mail.routes is not None
        return MailSettings(
            accounts=self.mail_accounts,
            routes=self.mail.routes.resolve(),
            state=self.mail.state.resolve(),
        )

    @property
    def revisit_settings(self) -> RevisitSettings:
        return RevisitSettings(
            state=self.revisits.state.resolve(),
            poll_interval_seconds=self.revisits.poll_interval_seconds,
        )

    @property
    def stewardship_settings(self) -> StewardshipSettings:
        return StewardshipSettings(
            enabled=self.stewardship.enabled,
            timezone=self.stewardship.timezone,
            waking_start=self.stewardship.waking_start,
            waking_end=self.stewardship.waking_end,
            state=self.stewardship.state.resolve(),
            poll_interval_seconds=self.stewardship.poll_interval_seconds,
        )


@dataclass(frozen=True, slots=True)
class MailSettings:
    """The complete enabled provider-neutral Mail runtime configuration."""

    accounts: tuple[MailAccountSettings, ...]
    routes: Path
    state: Path

    @property
    def username(self) -> str:
        """Preserve the historical single-iCloud settings interface."""
        account = next(item for item in self.accounts if item.provider == "icloud")
        return account.address

    @property
    def app_password(self) -> SecretStr:
        """Preserve the historical single-iCloud settings interface."""
        account = next(item for item in self.accounts if item.provider == "icloud")
        assert account.password is not None
        return account.password


@dataclass(frozen=True, slots=True)
class MailAccountSettings:
    """One stable mailbox identity and its private authentication material."""

    key: str
    label: str
    provider: Literal["icloud", "outlook"]
    address: str
    host: str
    password: SecretStr | None = None
    client_id: str | None = None
    token_cache: Path | None = None


@dataclass(frozen=True, slots=True)
class RevisitSettings:
    """The complete always-on revisit runtime configuration."""

    state: Path
    poll_interval_seconds: int


@dataclass(frozen=True, slots=True)
class StewardshipSettings:
    """Complete local runtime settings for the daily creative pulse."""

    enabled: bool
    timezone: str
    waking_start: daytime
    waking_end: daytime
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
        "workspace": str(settings.agent_workspace),
        "personality": str(settings.personality) if settings.personality else None,
        "telegram": {
            "bot_token": "<redacted>",
            "allowed_user_id": settings.allowed_user_id,
            "state": str(settings.telegram.state),
            "voice_transcription_command": (
                list(settings.telegram.voice_transcription_command)
                if settings.telegram.voice_transcription_command is not None
                else None
            ),
            "voice_transcription_timeout_seconds": (
                settings.telegram.voice_transcription_timeout_seconds
            ),
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
            "mail_label": settings.icloud.mail_label,
        },
        "outlook": {
            "enabled": settings.outlook.enabled,
            "address": settings.outlook.address,
            "client_id": settings.outlook.client_id,
            "token_cache": str(settings.outlook.token_cache),
            "label": settings.outlook.label,
        },
        "mail": {
            "enabled": settings.mail.enabled,
            "username": settings.mail.username,
            "app_password": (
                "<redacted>" if settings.mail.app_password is not None else None
            ),
            "routes": str(settings.mail.routes) if settings.mail.routes else None,
            "state": str(settings.mail.state),
            "accounts": [
                {
                    "key": account.key,
                    "label": account.label,
                    "provider": account.provider,
                    "address": account.address,
                    "host": account.host,
                    "token_cache": (
                        str(account.token_cache)
                        if account.token_cache is not None
                        else None
                    ),
                }
                for account in settings.mail_accounts
            ]
            if settings.mail.enabled
            else [],
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
        "browser": {
            "enabled": settings.browser.enabled,
            "state": str(settings.browser.state),
            "profiles": str(settings.browser.profiles),
            "artifacts": str(settings.browser.artifacts),
            "socket": str(settings.browser.socket),
            "headless": settings.browser.headless,
            "executable_path": (
                str(settings.browser.executable_path)
                if settings.browser.executable_path is not None
                else None
            ),
            "takeover_url": (
                str(settings.browser.takeover_url)
                if settings.browser.takeover_url is not None
                else None
            ),
            "lease_seconds": settings.browser.lease_seconds,
            "action_timeout_seconds": settings.browser.action_timeout_seconds,
            "observation_max_chars": settings.browser.observation_max_chars,
            "observation_max_elements": settings.browser.observation_max_elements,
            "journal_retention_days": settings.browser.journal_retention_days,
            "journal_max_entries": settings.browser.journal_max_entries,
        },
        "grocery": {
            "enabled": settings.grocery.enabled,
            "state": str(settings.grocery.state),
            "browser_profile": settings.grocery.browser_profile,
            "base_url": str(settings.grocery.base_url),
            "checkout_enabled": settings.grocery.checkout_enabled,
            "weighted_tolerance": str(settings.grocery.weighted_tolerance),
            "approval_ttl_seconds": settings.grocery.approval_ttl_seconds,
        },
        "revisits": {
            "state": str(settings.revisits.state),
            "poll_interval_seconds": settings.revisits.poll_interval_seconds,
        },
        "stewardship": {
            "enabled": settings.stewardship.enabled,
            "timezone": settings.stewardship.timezone,
            "waking_start": settings.stewardship.waking_start.isoformat(
                timespec="minutes"
            ),
            "waking_end": settings.stewardship.waking_end.isoformat(timespec="minutes"),
            "state": str(settings.stewardship.state),
            "poll_interval_seconds": settings.stewardship.poll_interval_seconds,
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
