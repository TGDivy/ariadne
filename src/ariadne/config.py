"""Environment-backed configuration for Ariadne."""

from pathlib import Path

from pydantic import DirectoryPath, Field, PositiveInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The small set of configuration values needed for Milestone 1."""

    model_config = SettingsConfigDict(env_file=None, hide_input_in_errors=True)

    telegram_bot_token: str = Field(
        min_length=1,
        validation_alias="TELEGRAM_BOT_TOKEN",
    )
    allowed_user_id: PositiveInt = Field(
        validation_alias="TELEGRAM_ALLOWED_USER_ID",
    )
    workspace: DirectoryPath = Field(validation_alias="ARIADNE_WORKSPACE")

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def strip_bot_token(cls, value: object) -> object:
        """Treat whitespace-only tokens as absent."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("workspace", mode="before")
    @classmethod
    def expand_workspace(cls, value: object) -> object:
        """Expand a user-relative workspace path before validating it."""
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("Workspace path must not be empty.")
            return Path(value).expanduser()
        return value

    @field_validator("workspace")
    @classmethod
    def resolve_workspace(cls, value: Path) -> Path:
        """Store the workspace as an absolute path."""
        return value.resolve()
