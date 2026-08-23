"""Codex turn profile for one mail event."""

from pathlib import Path

from ..codex.models import CodexTurnSettings, TurnProfile
from ..codex.profile import COMMON_IRIS_TOOLS, MAIL_TOOL, resolve_profile


def mail_profile(
    vault: Path,
    settings: CodexTurnSettings,
    *,
    human: str,
    job_id: str | None = None,
    state: Path | None = None,
) -> TurnProfile:
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
        name="mail",
        surface_package=__package__,
        vault=vault,
        settings=settings,
        human=human,
        enabled_tools=(*COMMON_IRIS_TOOLS, MAIL_TOOL),
        thread_policy="fresh-per-event",
        mcp_environment=environment,
        mcp_environment_names=("ARIADNE_MAIL_JOB_ID", "ARIADNE_MAIL_STATE"),
    )
