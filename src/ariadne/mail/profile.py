"""Declarative Codex turn profile for mail events."""

from pathlib import Path

from openai_codex.generated.v2_all import ReasoningEffort

from ..codex.models import CodexTurnSettings, ResolvedTurnProfile, TurnProfile
from ..codex.profile import NETWORK_DOMAINS, resolve_profile

MAIL_PROFILE = TurnProfile(
    name="mail",
    model="gpt-5.6-luna",
    effort=ReasoningEffort.medium,
    web_search="disabled",
    instruction_documents=("base", "mail"),
    developer_documents=("grounding", "ariadne"),
    enabled_tools=(
        "runtime_status",
        "send_message",
        "react",
        "prepare_files",
        "triage_current_mail",
    ),
    thread_policy="fresh-per-event",
    network_domains=NETWORK_DOMAINS,
    mcp_environment_names=(
        "ARIADNE_VAULT",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
        "ARIADNE_MAIL_JOB_ID",
        "ARIADNE_MAIL_STATE",
    ),
)


def resolve_mail_profile(
    vault: Path,
    settings: CodexTurnSettings | None = None,
    *,
    human: str,
    job_id: str | None = None,
    state: Path | None = None,
) -> ResolvedTurnProfile:
    """Resolve mail defaults or independent configured overrides for one job."""
    if (job_id is None) != (state is None):
        raise ValueError("Mail job id and state path must be provided together.")
    environment = (
        {
            "ARIADNE_MAIL_JOB_ID": job_id,
            "ARIADNE_MAIL_STATE": str(state),
        }
        if job_id is not None and state is not None
        else {}
    )
    return resolve_profile(
        MAIL_PROFILE,
        vault=vault,
        settings=settings,
        human=human,
        mcp_environment=environment,
    )
