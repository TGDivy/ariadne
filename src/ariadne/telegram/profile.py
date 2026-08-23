"""Codex turn profile for Telegram conversations."""

from pathlib import Path

from ..codex.models import CodexTurnSettings, TurnProfile
from ..codex.profile import COMMON_IRIS_TOOLS, resolve_profile


def telegram_profile(
    vault: Path, settings: CodexTurnSettings, *, human: str
) -> TurnProfile:
    return resolve_profile(
        name="telegram",
        surface_package=__package__,
        vault=vault,
        settings=settings,
        human=human,
        enabled_tools=COMMON_IRIS_TOOLS,
        thread_policy="shared",
    )
