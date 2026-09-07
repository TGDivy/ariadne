"""Ariadne's primary entry point and service loop."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import suppress
from pathlib import Path

from .bot import AriadneApplication, AriadneBot
from .codex.resolver import resolve_profile
from .config import load_settings
from .mail import MailLoop
from .profile import PROFILES, TELEGRAM_PROFILE
from .revisit import RevisitLoop
from .terminal import Codex, CodexConversation
from .telemetry import TelemetryClient, configure_telemetry

LOGGER = logging.getLogger("ariadne")


def configure_logging() -> None:
    """Suppress verbose HTTP client logs and configure the root logger."""
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    httpx_logger.setLevel(logging.WARNING)
    httpcore_logger.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)


def _load_configuration(path: Path | None = None) -> object:
    """Load validated configuration, showing errors on failure."""
    try:
        return load_settings(path)
    except ValueError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error


def _configure_cli_environment(path: Path | None) -> None:
    """Make the selected config and sibling console script visible to Codex."""
    from .config import config_path
    os.environ["ARIADNE_CONFIG"] = str(config_path(path))
    # Resolve the directory, not the Python symlink, so a virtualenv's sibling
    # `ariadne` console script is not accidentally replaced by /usr/bin.
    executable_directory = str(Path(sys.executable).parent.resolve())
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    if executable_directory not in entries:
        os.environ["PATH"] = os.pathsep.join((executable_directory, *entries))


def run(path: Path | None = None) -> None:
    """Start Ariadne using the selected private TOML configuration."""
    configure_logging()
    settings = _load_configuration(path)
    # The model process inherits this path; provider credentials remain in the
    # private file and are loaded by the `ariadne` CLI on demand.
    _configure_cli_environment(path)
    mail_settings = settings.mail_settings
    telemetry = configure_telemetry(settings.telemetry)

    conversation = CodexConversation(
        resolve_profile(
            TELEGRAM_PROFILE,
            vault=settings.codex_root,
            settings=settings.codex_turn_settings,
            human=settings.human_name,
            personality=settings.personality,
            mcp_environment=settings.mcp_environment,
            knowledge_root=settings.knowledge_root,
            network_domains=settings.health_network_domains,
        ),
        telemetry=telemetry,
    )
    ariadne = AriadneBot(
        settings.allowed_user_id,
        conversation,
        bot_token=settings.telegram_bot_token,
        question_state=settings.telegram.state.resolve(),
    )
    try:
        mail_loop = (
            MailLoop(
                mail_settings,
                settings.codex_root,
                settings.mail_turn_settings,
                human=settings.human_name,
                personality=settings.personality,
                mcp_environment=settings.mcp_environment,
                knowledge_root=settings.knowledge_root,
                network_domains=settings.health_network_domains,
                telemetry=telemetry,
            )
            if mail_settings is not None
            else None
        )
        revisit_loop = RevisitLoop(
            settings.revisit_settings,
            settings.codex_root,
            settings.revisit_turn_settings,
            human=settings.human_name,
            personality=settings.personality,
            mcp_environment=settings.mcp_environment,
            knowledge_root=settings.knowledge_root,
            network_domains=settings.health_network_domains,
            telemetry=telemetry,
        )
    except ValueError as error:
        telemetry.shutdown()
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error
    mail_task: asyncio.Task[None] | None = None
    revisit_task: asyncio.Task[None] | None = None

    async def start_services(application: AriadneApplication) -> None:
        nonlocal mail_task, revisit_task
        ariadne.bind_bot(application.bot)
        await ariadne.recover_questions()
        await publish_commands(application)
        if mail_loop is not None:
            mail_task = asyncio.create_task(mail_loop.run_forever())
            LOGGER.info("Started iCloud Mail source")
        revisit_task = asyncio.create_task(revisit_loop.run_forever())
        LOGGER.info("Started one-off revisit source")

    async def close_services(_: object) -> None:
        if mail_loop is not None:
            mail_loop.stop()
        revisit_loop.stop()
        if mail_task is not None:
            mail_task.cancel()
            with suppress(asyncio.CancelledError):
                await mail_task
        if revisit_task is not None:
            revisit_task.cancel()
            with suppress(asyncio.CancelledError):
                await revisit_task
        await ariadne.close()
        telemetry.shutdown()

    application = AriadneApplication(
        start_services=start_services, close_services=close_services
    )
    application.run_polling()


async def publish_commands(application: AriadneApplication) -> None:
    """Register Telegram bot commands for the default help text."""
    from telegram import BotCommand, BotCommandScope

    await application.bot.set_my_commands(
        [
            BotCommand("new", "Start a fresh Codex conversation"),
            BotCommand("settings", "Configure model, reasoning, and research"),
            BotCommand("stop", "Ask the active turn to interrupt"),
        ],
        scope=BotCommandScope(),
    )
