"""Run Ariadne's Telegram conversation and optional background loops."""

import asyncio
import logging
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from .codex import CodexConversation
from .codex.resolver import resolve_profile
from .config import CONFIG_PATH_ENVIRONMENT, Settings, config_path, load_settings
from .mail import MailLoop
from .profile import TELEGRAM_PROFILE
from .revisit.runtime import RevisitLoop
from .telegram.bot import AriadneBot
from .telemetry import configure_telemetry

LOGGER = logging.getLogger(__name__)

AriadneApplication = Application[Any, Any, Any, Any, Any, Any]

COMMANDS = (
    BotCommand("new", "Start a fresh conversation"),
    BotCommand("stop", "Interrupt the turn Ariadne is working on"),
    BotCommand("settings", "Model, reasoning effort, and web research"),
)


async def publish_commands(application: AriadneApplication) -> None:
    """Put Ariadne's commands in Telegram's menu so they can be found."""
    try:
        await application.bot.set_my_commands(COMMANDS)
    except Exception:
        LOGGER.exception("Telegram command menu could not be published")


def configure_logging() -> None:
    """Configure Ariadne logs without exposing Telegram request URLs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load_configuration(path: Path | None) -> Settings:
    try:
        return load_settings(path)
    except ValidationError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error
    except ValueError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error


def _configure_cli_environment(path: Path | None) -> None:
    """Make the selected config and sibling console script visible to Codex."""
    os.environ[CONFIG_PATH_ENVIRONMENT] = str(config_path(path))
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
            vault=settings.vault,
            settings=settings.codex_turn_settings,
            human=settings.human_name,
            personality=settings.personality,
            mcp_environment=settings.mcp_environment,
            knowledge_root=settings.vault,
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
                settings.vault,
                settings.mail_turn_settings,
                human=settings.human_name,
                personality=settings.personality,
                mcp_environment=settings.mcp_environment,
                telemetry=telemetry,
            )
            if mail_settings is not None
            else None
        )
        revisit_loop = RevisitLoop(
            settings.revisit_settings,
            settings.vault,
            settings.revisit_turn_settings,
            human=settings.human_name,
            personality=settings.personality,
            mcp_environment=settings.mcp_environment,
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
        try:
            await conversation.close()
        except Exception:
            LOGGER.exception("Failed to close Codex client")
        await asyncio.to_thread(telemetry.shutdown)

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_init(start_services)
        .post_shutdown(close_services)
        .build()
    )
    application.add_handler(CommandHandler("start", ariadne.start))
    application.add_handler(CommandHandler("new", ariadne.new))
    application.add_handler(CommandHandler("stop", ariadne.stop))
    application.add_handler(CommandHandler("settings", ariadne.settings))
    application.add_handler(
        CallbackQueryHandler(ariadne.settings_callback, pattern=r"^settings:")
    )
    application.add_handler(
        CallbackQueryHandler(ariadne.file_delivery_callback, pattern=r"^file-delivery:")
    )
    application.add_handler(
        CallbackQueryHandler(ariadne.question_callback, pattern=r"^question:")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, ariadne.text)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, ariadne.image)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, ariadne.document)
    )
    # PTB 22.8 predates Rich Messages, but retains the raw field in
    # Message.api_kwargs. A second handler group lets us inspect those updates
    # without competing with the native text/media handlers above.
    application.add_handler(TypeHandler(Update, ariadne.rich_message), group=1)

    LOGGER.info("Starting Ariadne with private knowledge at %s", settings.vault)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        LOGGER.exception("Telegram polling stopped unexpectedly")
        raise
