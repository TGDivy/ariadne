"""Run Ariadne's Telegram conversation and optional mail loops."""

import argparse
import asyncio
import json
import logging
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
    filters,
)

from .codex import CodexConversation
from .codex.resolver import resolve_profile
from .config import Settings, config_path, load_settings, settings_payload
from .mail import MailLoop
from .profile import TELEGRAM_PROFILE
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


def main() -> None:
    """Start Ariadne or inspect its typed configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Path to Ariadne's TOML config")
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Validate configuration")
    config_parser.add_argument("action", choices=("check", "show"))
    args = parser.parse_args()

    configure_logging()
    settings = _load_configuration(args.config)
    if args.command == "config":
        if args.action == "show":
            print(json.dumps(settings_payload(settings), indent=2))
        else:
            print(f"Configuration is valid: {config_path(args.config)}")
        return

    mail_settings = settings.mail_settings
    telemetry = configure_telemetry()

    conversation = CodexConversation(
        resolve_profile(
            TELEGRAM_PROFILE,
            vault=settings.vault,
            settings=settings.codex_turn_settings,
            human=settings.human_name,
            mcp_environment=settings.mcp_environment,
        ),
        telemetry=telemetry,
    )
    ariadne = AriadneBot(
        settings.allowed_user_id,
        conversation,
        bot_token=settings.telegram_bot_token,
    )
    try:
        mail_loop = (
            MailLoop(
                mail_settings,
                settings.vault,
                settings.mail_turn_settings,
                human=settings.human_name,
                mcp_environment=settings.mcp_environment,
                telemetry=telemetry,
            )
            if mail_settings is not None
            else None
        )
    except ValueError as error:
        telemetry.shutdown()
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error
    mail_task: asyncio.Task[None] | None = None

    async def start_services(application: AriadneApplication) -> None:
        nonlocal mail_task
        await publish_commands(application)
        if mail_loop is not None:
            mail_task = asyncio.create_task(mail_loop.run_forever())
            LOGGER.info("Started iCloud Mail source")

    async def close_services(_: object) -> None:
        if mail_loop is not None:
            mail_loop.stop()
        if mail_task is not None:
            mail_task.cancel()
            with suppress(asyncio.CancelledError):
                await mail_task
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
        MessageHandler(filters.TEXT & ~filters.COMMAND, ariadne.text)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, ariadne.image)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, ariadne.document)
    )

    LOGGER.info("Starting Ariadne with The Thread vault %s", settings.vault)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        LOGGER.exception("Telegram polling stopped unexpectedly")
        raise


if __name__ == "__main__":
    main()
