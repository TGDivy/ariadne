"""Typed TOML configuration for Ariadne."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import (
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
from .profile import PROFILES

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


class MailConfig(BaseModel):
    """Opt-in iCloud Mail configuration."""

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
        if not self.enabled:
            return self
        missing = []
        if self.username is None:
            missing.append("username")
        if (
            self.app_password is None
            or not self.app_password.get_secret_value().strip()
        ):
            missing.append("app_password")
        if self.routes is None:
            missing.append("routes")
        if missing:
            raise ValueError("Enabled mail requires: " + ", ".join(missing) + ".")
        assert self.routes is not None
        if not self.routes.is_file():
            raise ValueError("Mail routes must point to a YAML file.")
        return self


class Settings(BaseModel):
    """Complete validated Ariadne configuration."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    version: Literal[1]
    human_name: str = Field(min_length=1)
    vault: DirectoryPath
    telegram: TelegramConfig
    mail: MailConfig = Field(default_factory=MailConfig)
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

    @model_validator(mode="after")
    def reject_unknown_profiles(self) -> Settings:
        unknown = self.profiles.keys() - PROFILES.keys()
        if unknown:
            raise ValueError("Unknown turn profiles: " + ", ".join(sorted(unknown)))
        return self

    @property
    def telegram_bot_token(self) -> str:
        return self.telegram.bot_token.get_secret_value()

    @property
    def allowed_user_id(self) -> int:
        return self.telegram.allowed_user_id

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
    def mcp_environment(self) -> dict[str, str]:
        return {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_ALLOWED_USER_ID": str(self.allowed_user_id),
        }

    @property
    def mail_settings(self) -> MailSettings | None:
        if not self.mail.enabled:
            return None
        assert self.mail.username is not None
        assert self.mail.app_password is not None
        assert self.mail.routes is not None
        return MailSettings(
            username=self.mail.username,
            app_password=self.mail.app_password,
            routes=self.mail.routes.resolve(),
            state=self.mail.state.resolve(),
        )


@dataclass(frozen=True, slots=True)
class MailSettings:
    """The complete enabled iCloud Mail runtime configuration."""

    username: str
    app_password: SecretStr
    routes: Path
    state: Path


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
        "telegram": {
            "bot_token": "<redacted>",
            "allowed_user_id": settings.allowed_user_id,
            "identity": {
                "name": identity.name,
                "description": identity.description,
                "short_description": identity.short_description,
                "profile_photo": (
                    str(identity.profile_photo) if identity.profile_photo else None
                ),
            },
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
        "profiles": {
            name: {
                "model": settings.turn_settings(name).model,
                "effort": settings.turn_settings(name).effort.value,
                "web_search": settings.turn_settings(name).web_search,
            }
            for name in PROFILES
        },
    }
