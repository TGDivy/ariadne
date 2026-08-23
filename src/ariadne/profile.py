"""All declarative Codex turn profiles used by Ariadne."""

from pathlib import Path

from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ReasoningEffort

from .codex.models import TurnProfile

NETWORK_DOMAINS = (
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "cdn.playwright.dev",
    "playwright.azureedge.net",
    "localhost",
    "127.0.0.1",
)

TELEGRAM_PROFILE = TurnProfile(
    name="telegram",
    model="gpt-5.6-luna",
    effort=ReasoningEffort.low,
    web_search="disabled",
    instruction_documents=("base", "telegram"),
    developer_documents=("grounding", "ariadne"),
    enabled_tools=("runtime_status", "send_message", "react", "prepare_files"),
    thread_policy="shared",
    sandbox=Sandbox.workspace_write,
    approval_mode=ApprovalMode.auto_review,
    permission_profile="ariadne",
    writable_roots=(Path.home(),),
    network_domains=NETWORK_DOMAINS,
    allow_local_binding=True,
    mcp_environment_names=(
        "ARIADNE_VAULT",
        "ARIADNE_PROFILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
    ),
)

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
    sandbox=Sandbox.workspace_write,
    approval_mode=ApprovalMode.auto_review,
    permission_profile="ariadne",
    writable_roots=(Path.home(),),
    network_domains=NETWORK_DOMAINS,
    allow_local_binding=True,
    mcp_environment_names=(
        "ARIADNE_VAULT",
        "ARIADNE_PROFILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
        "ARIADNE_MAIL_JOB_ID",
        "ARIADNE_MAIL_STATE",
    ),
)

PROFILES: dict[str, TurnProfile] = {
    profile.name: profile for profile in (TELEGRAM_PROFILE, MAIL_PROFILE)
}
