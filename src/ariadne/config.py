"""Environment-backed configuration for Ariadne."""
# ruff: noqa: E501

from dataclasses import dataclass
from pathlib import Path

from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import DirectoryPath, Field, PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .codex import CodexTurnSettings, WebSearchSetting


@dataclass(frozen=True, slots=True)
class GitHubWebhookSettings:
    secret: str
    allowed_repositories: frozenset[str]
    host: str
    port: PositiveInt


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
    github_webhook_secret: SecretStr | None = Field(default=None, validation_alias="ARIADNE_GITHUB_WEBHOOK_SECRET")
    github_webhook_repositories: str = Field(default="", validation_alias="ARIADNE_GITHUB_WEBHOOK_REPOSITORIES")
    github_webhook_host: str = Field(default="127.0.0.1", min_length=1, validation_alias="ARIADNE_GITHUB_WEBHOOK_HOST")
    github_webhook_port: PositiveInt = Field(default=8787, validation_alias="ARIADNE_GITHUB_WEBHOOK_PORT")

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

    @property
    def github_webhook_settings(self) -> GitHubWebhookSettings:
        secret = self.github_webhook_secret.get_secret_value() if self.github_webhook_secret else ""
        repositories = frozenset(name.strip() for name in self.github_webhook_repositories.split(",") if name.strip())
        if not secret:
            raise ValueError("ARIADNE_GITHUB_WEBHOOK_SECRET must be set.")
        if not repositories:
            raise ValueError("ARIADNE_GITHUB_WEBHOOK_REPOSITORIES must list a repository.")
        return GitHubWebhookSettings(secret, repositories, self.github_webhook_host, self.github_webhook_port)
