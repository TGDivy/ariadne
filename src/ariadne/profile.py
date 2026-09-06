"""All declarative Codex turn profiles used by Ariadne."""

from pathlib import Path

from openai_codex import ApprovalMode
from openai_codex.generated.v2_all import ReasoningEffort

from .codex.models import TurnProfile
from .knowledge.capability import ROOT_ENVIRONMENT as KNOWLEDGE_ROOT_ENVIRONMENT
from .knowledge.capability import TOOLS as KNOWLEDGE_TOOLS
from .revisit import ATTENTION_SETTINGS, Attention
from .revisit import STATE_ENVIRONMENT as REVISIT_STATE_ENVIRONMENT
from .revisit import TOOLS as REVISIT_TOOLS

NETWORK_DOMAINS = (
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "cdn.playwright.dev",
    "playwright.azureedge.net",
    "imap.mail.me.com",
    "caldav.icloud.com",
    "*.icloud.com",
    "localhost",
    "127.0.0.1",
)

TELEGRAM_PROFILE = TurnProfile(
    name="telegram",
    model="gpt-5.6-luna",
    effort=ReasoningEffort.high,
    web_search="disabled",
    instruction_documents=("base", "telegram", "knowledge"),
    developer_documents=("grounding", "companion"),
    enabled_tools=(
        "read_recent_telegram_messages",
        "ask_telegram_question",
        "request_telegram_file_delivery",
        *REVISIT_TOOLS,
        *KNOWLEDGE_TOOLS,
    ),
    thread_policy="shared",
    reasoning_summary="concise",
    approval_mode=ApprovalMode.auto_review,
    permission_profile="ariadne",
    writable_roots=(Path.home(),),
    network_domains=NETWORK_DOMAINS,
    allow_local_binding=True,
    mcp_environment_names=(
        "ARIADNE_PROFILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
        "ARIADNE_TELEGRAM_STATE",
        REVISIT_STATE_ENVIRONMENT,
        KNOWLEDGE_ROOT_ENVIRONMENT,
    ),
)

MAIL_PROFILE = TurnProfile(
    name="mail",
    model="gpt-5.6-terra",
    effort=ReasoningEffort.medium,
    web_search="live",
    instruction_documents=("base", "mail", "knowledge"),
    developer_documents=("grounding", "companion"),
    enabled_tools=(
        "send_telegram_message",
        "read_recent_telegram_messages",
        "request_telegram_file_delivery",
        "record_current_mail_decision",
        *REVISIT_TOOLS,
        *KNOWLEDGE_TOOLS,
    ),
    thread_policy="fresh-per-event",
    approval_mode=ApprovalMode.auto_review,
    permission_profile="ariadne",
    writable_roots=(Path.home(),),
    network_domains=NETWORK_DOMAINS,
    allow_local_binding=True,
    mcp_environment_names=(
        "ARIADNE_PROFILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_ID",
        "ARIADNE_TELEGRAM_STATE",
        "ARIADNE_MAIL_JOB_ID",
        "ARIADNE_MAIL_STATE",
        REVISIT_STATE_ENVIRONMENT,
        KNOWLEDGE_ROOT_ENVIRONMENT,
    ),
)


def _revisit_profile(attention: Attention) -> TurnProfile:
    settings = ATTENTION_SETTINGS[attention]
    return TurnProfile(
        name=f"revisit-{attention.value}",
        model=settings.model,
        effort=settings.effort,
        web_search=settings.web_search,
        instruction_documents=("base", "revisit", "knowledge"),
        developer_documents=("grounding", "companion"),
        enabled_tools=(
            "send_telegram_message",
            "read_recent_telegram_messages",
            *REVISIT_TOOLS,
            *KNOWLEDGE_TOOLS,
        ),
        thread_policy="fresh-per-event",
        approval_mode=ApprovalMode.auto_review,
        permission_profile="ariadne",
        writable_roots=(Path.home(),),
        network_domains=NETWORK_DOMAINS,
        allow_local_binding=True,
        mcp_environment_names=(
            "ARIADNE_PROFILE",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USER_ID",
            "ARIADNE_TELEGRAM_STATE",
            REVISIT_STATE_ENVIRONMENT,
            KNOWLEDGE_ROOT_ENVIRONMENT,
        ),
    )


REVISIT_PROFILES: dict[Attention, TurnProfile] = {
    attention: _revisit_profile(attention) for attention in Attention
}


def profile_for_attention(attention: Attention) -> TurnProfile:
    """Return the exact profile declared for a selected attention level."""
    return REVISIT_PROFILES[attention]


PROFILES: dict[str, TurnProfile] = {
    profile.name: profile
    for profile in (
        TELEGRAM_PROFILE,
        MAIL_PROFILE,
        *REVISIT_PROFILES.values(),
    )
}
