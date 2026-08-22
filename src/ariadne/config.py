"""Environment-backed configuration for Ariadne."""

from pathlib import Path

from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import DirectoryPath, Field, PositiveInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .codex import CodexTurnSettings, WebSearchSetting


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
    codex_model: str = Field(
        default="gpt-5.6-luna",
        min_length=1,
        validation_alias="ARIADNE_CODEX_MODEL",
    )
    reasoning_effort: ReasoningEffort = Field(
        default=ReasoningEffort.low,
        validation_alias="ARIADNE_REASONING_EFFORT",
    )
    web_search: WebSearchSetting = Field(
        default="disabled",
        validation_alias="ARIADNE_WEB_SEARCH",
    )

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def strip_bot_token(cls, value: object) -> object:
        """Treat whitespace-only tokens as absent."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("codex_model", mode="before")
    @classmethod
    def strip_codex_model(cls, value: object) -> object:
        """Treat whitespace-only model names as absent."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("human_name", mode="before")
    @classmethod
    def strip_human_name(cls, value: object) -> object:
        """Treat a whitespace-only name as absent."""
        return value.strip() if isinstance(value, str) else value

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
        """Return the explicit Codex settings for this Ariadne process."""
        return CodexTurnSettings(
            model=self.codex_model,
            effort=self.reasoning_effort,
            web_search=self.web_search,
        )
