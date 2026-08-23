"""Environment-backed configuration for Ariadne."""

from dataclasses import dataclass
from pathlib import Path

from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import DirectoryPath, Field, PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .codex.models import CodexTurnSettings, WebSearchSetting


@dataclass(frozen=True, slots=True)
class MailSettings:
    """The complete opt-in iCloud Mail runtime configuration."""

    username: str
    app_password: SecretStr
    routes: Path
    state: Path


class Settings(BaseSettings):
    """Small validated configuration for Ariadne."""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    telegram_bot_token: str = Field(
        min_length=1,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )
    allowed_user_id: PositiveInt = Field(
        validation_alias="TELEGRAM_ALLOWED_USER_ID",
    )
    human_name: str = Field(
        min_length=1,
        validation_alias="ARIADNE_HUMAN_NAME",
    )
    vault: DirectoryPath = Field(validation_alias="ARIADNE_VAULT")
    codex_model: str | None = Field(
        default=None,
        min_length=1,
        validation_alias="ARIADNE_CODEX_MODEL",
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        validation_alias="ARIADNE_REASONING_EFFORT",
    )
    web_search: WebSearchSetting | None = Field(
        default=None,
        validation_alias="ARIADNE_WEB_SEARCH",
    )
    mail_model: str | None = Field(
        default=None,
        min_length=1,
        validation_alias="ARIADNE_MAIL_MODEL",
    )
    mail_reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        validation_alias="ARIADNE_MAIL_REASONING_EFFORT",
    )
    mail_web_search: WebSearchSetting | None = Field(
        default=None,
        validation_alias="ARIADNE_MAIL_WEB_SEARCH",
    )
    icloud_username: str | None = Field(
        default=None,
        validation_alias="ICLOUD_USERNAME",
    )
    icloud_app_password: SecretStr | None = Field(
        default=None,
        validation_alias="ICLOUD_APP_PASSWORD",
    )
    mail_routes: Path | None = Field(
        default=None,
        validation_alias="ARIADNE_MAIL_ROUTES",
    )
    mail_state: Path = Field(
        default=Path("~/.local/state/ariadne/mail.sqlite3"),
        validation_alias="ARIADNE_MAIL_STATE",
    )

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def strip_bot_token(cls, value: object) -> object:
        """Treat whitespace-only tokens as absent."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("codex_model", "mail_model", mode="before")
    @classmethod
    def strip_codex_model(cls, value: object) -> object:
        """Treat whitespace-only model names as absent."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("human_name", mode="before")
    @classmethod
    def strip_human_name(cls, value: object) -> object:
        """Treat a whitespace-only name as absent."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("icloud_username", mode="before")
    @classmethod
    def strip_icloud_username(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("mail_routes", "mail_state", mode="before")
    @classmethod
    def expand_mail_path(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return Path(value).expanduser() if value else None
        if isinstance(value, Path):
            return value.expanduser()
        return value

    @field_validator("vault", mode="before")
    @classmethod
    def expand_vault(cls, value: object) -> object:
        """Expand a user-relative vault path before validating it."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Vault path must not be empty.")
            return Path(value).expanduser()
        return value

    @field_validator("vault")
    @classmethod
    def validate_vault(cls, value: Path) -> Path:
        """Require The Thread to be a local Git repository."""
        vault = value.resolve()
        if not (vault / ".git").exists():
            raise ValueError("ARIADNE_VAULT must point to a Git repository.")
        return vault

    @property
    def codex_turn_settings(self) -> CodexTurnSettings:
        """Overlay Telegram's environment settings on its declared defaults."""
        from .telegram.profile import TELEGRAM_PROFILE

        return CodexTurnSettings(
            model=self.codex_model or TELEGRAM_PROFILE.model,
            effort=self.reasoning_effort or TELEGRAM_PROFILE.effort,
            web_search=self.web_search or TELEGRAM_PROFILE.web_search,
        )

    @property
    def mail_turn_settings(self) -> CodexTurnSettings:
        """Overlay mail's environment settings on its declared defaults."""
        from .mail.profile import MAIL_PROFILE

        return CodexTurnSettings(
            model=self.mail_model or MAIL_PROFILE.model,
            effort=self.mail_reasoning_effort or MAIL_PROFILE.effort,
            web_search=self.mail_web_search or MAIL_PROFILE.web_search,
        )

    @property
    def mail_settings(self) -> MailSettings | None:
        """Return mail settings when the source is configured completely."""
        configured = (
            self.icloud_username is not None,
            self.icloud_app_password is not None,
            self.mail_routes is not None,
        )
        if not any(configured):
            return None
        if not all(configured):
            raise ValueError(
                "ICLOUD_USERNAME, ICLOUD_APP_PASSWORD, and ARIADNE_MAIL_ROUTES "
                "must be set together."
            )
        assert self.icloud_username is not None
        assert self.icloud_app_password is not None
        assert self.mail_routes is not None
        routes = self.mail_routes.resolve()
        if not routes.is_file():
            raise ValueError("ARIADNE_MAIL_ROUTES must point to a YAML file.")
        return MailSettings(
            username=self.icloud_username,
            app_password=self.icloud_app_password,
            routes=routes,
            state=self.mail_state.resolve(),
        )
