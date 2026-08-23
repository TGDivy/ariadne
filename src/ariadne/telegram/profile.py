"""Declarative Codex turn profile for Telegram conversations."""

from pathlib import Path

from openai_codex.generated.v2_all import ReasoningEffort

from ..codex.models import CodexTurnSettings, ResolvedTurnProfile, TurnProfile
from ..codex.profile import NETWORK_DOMAINS, resolve_profile

TELEGRAM_PROFILE = TurnProfile(
    name="telegram",
    model="gpt-5.6-luna",
    effort=ReasoningEffort.low,
    web_search="disabled",
    instruction_documents=("base", "telegram"),
    developer_documents=("grounding", "ariadne"),
    enabled_tools=("runtime_status", "send_message", "react", "prepare_files"),
    thread_policy="shared",
    network_domains=NETWORK_DOMAINS,
    mcp_environment_names=(
        "ARIADNE_VAULT",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
    ),
)


def resolve_telegram_profile(
    vault: Path,
    settings: CodexTurnSettings,
    *,
    human: str,
) -> ResolvedTurnProfile:
    """Resolve the profile with Telegram's current `/settings` selection."""
    return resolve_profile(
        TELEGRAM_PROFILE,
        vault=vault,
        settings=settings,
        human=human,
    )
