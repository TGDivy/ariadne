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
    "caldav.icloud.com",
    "*.icloud.com",
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
    enabled_tools=(
        "runtime_status",
        "ask_telegram_question",
        "prepare_files",
        "search_mail",
        "read_mail",
        "read_mail_thread",
        "list_calendars",
        "search_calendar",
        "read_calendar_event",
        "calendar_free_busy",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "respond_to_calendar_invitation",
    ),
    thread_policy="shared",
    reasoning_summary="concise",
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
        "ARIADNE_TELEGRAM_STATE",
        "ARIADNE_MAIL_USERNAME",
        "ARIADNE_MAIL_APP_PASSWORD",
        "ARIADNE_ICLOUD_USERNAME",
        "ARIADNE_ICLOUD_APP_PASSWORD",
        "ARIADNE_CALENDAR_TIMEZONE",
        "ARIADNE_CALENDAR_DEFAULT",
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
        "send_telegram_message",
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
