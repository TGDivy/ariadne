import json
from pathlib import Path

from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ReasoningEffort

from ariadne.codex.models import CodexTurnSettings
from ariadne.mail.profile import mail_profile
from ariadne.scripts.profile import profile_payload, render_profile
from ariadne.telegram.profile import telegram_profile

TELEGRAM_SETTINGS = CodexTurnSettings(
    model="gpt-telegram",
    effort=ReasoningEffort.low,
    web_search="live",
)
MAIL_SETTINGS = CodexTurnSettings(
    model="gpt-5.6-luna",
    effort=ReasoningEffort.medium,
    web_search="disabled",
)


def test_telegram_profile_is_complete_and_uses_dynamic_settings(
    tmp_path: Path,
) -> None:
    profile = telegram_profile(tmp_path, TELEGRAM_SETTINGS, human="Divy")

    assert profile.name == "telegram"
    assert profile.settings == TELEGRAM_SETTINGS
    assert profile.thread_policy == "shared"
    assert profile.cwd == tmp_path
    assert profile.sandbox == Sandbox.workspace_write
    assert profile.approval_mode == ApprovalMode.auto_review
    assert profile.writable_roots == (Path.home(),)
    assert "github.com" in profile.network_domains
    assert profile.allow_local_binding is True
    assert profile.enabled_tools == (
        "runtime_status",
        "send_message",
        "react",
        "prepare_files",
    )
    assert "ariadne.telegram/instructions.md" in profile.base_instruction_sources
    assert "Live web search is enabled." in profile.developer_instructions


def test_mail_profile_has_independent_settings_and_mail_authority(
    tmp_path: Path,
) -> None:
    profile = mail_profile(
        tmp_path,
        MAIL_SETTINGS,
        human="Divy",
        job_id="INBOX:1:2",
        state=tmp_path / "mail.sqlite3",
    )

    assert profile.name == "mail"
    assert profile.settings == MAIL_SETTINGS
    assert profile.thread_policy == "fresh-per-event"
    assert profile.enabled_tools[-1] == "triage_current_mail"
    assert (
        "triage_current_mail"
        not in telegram_profile(tmp_path, TELEGRAM_SETTINGS, human="Divy").enabled_tools
    )
    assert "ARIADNE_MAIL_JOB_ID" in profile.mcp_environment_names
    assert "A mail event arrived." in profile.base_instructions


def test_profile_inspection_never_contains_environment_values(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token")
    profile = mail_profile(
        tmp_path,
        MAIL_SETTINGS,
        human="Divy",
        job_id="secret-job-id",
        state=tmp_path / "secret-state.sqlite3",
    )

    serialized = json.dumps(profile_payload(profile))
    rendered = render_profile(profile)

    assert "TELEGRAM_BOT_TOKEN" in serialized
    assert "ARIADNE_MAIL_JOB_ID" in serialized
    assert "super-secret-token" not in serialized
    assert "secret-job-id" not in serialized
    assert "secret-state.sqlite3" not in serialized
    assert "Profile: mail" in rendered
    assert '"permission_profile": "ariadne"' in serialized
    assert '"allow_local_binding": true' in serialized
