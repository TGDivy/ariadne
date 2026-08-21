"""Environment-backed configuration for Ariadne."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when Ariadne cannot start with the supplied configuration."""


@dataclass(frozen=True, slots=True)
class Settings:
    """The small set of configuration values needed for Milestone 1."""

    telegram_bot_token: str
    allowed_user_id: int
    workspace: Path

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "Settings":
        """Load and validate Ariadne's required environment variables."""
        values = os.environ if environment is None else environment
        required_names = (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USER_ID",
            "ARIADNE_WORKSPACE",
        )
        missing = [name for name in required_names if not values.get(name, "").strip()]
        if missing:
            names = ", ".join(missing)
            raise ConfigurationError(
                f"Missing required environment variable(s): {names}"
            )

        allowed_user_id_text = values["TELEGRAM_ALLOWED_USER_ID"].strip()
        try:
            allowed_user_id = int(allowed_user_id_text)
        except ValueError:
            raise ConfigurationError(
                "TELEGRAM_ALLOWED_USER_ID must be a positive integer."
            ) from None

        if allowed_user_id <= 0:
            raise ConfigurationError(
                "TELEGRAM_ALLOWED_USER_ID must be a positive integer."
            )

        workspace = Path(values["ARIADNE_WORKSPACE"].strip()).expanduser()
        if not workspace.is_dir():
            raise ConfigurationError(
                "ARIADNE_WORKSPACE must point to an existing directory."
            )

        return cls(
            telegram_bot_token=values["TELEGRAM_BOT_TOKEN"].strip(),
            allowed_user_id=allowed_user_id,
            workspace=workspace.resolve(),
        )
