"""Run Ariadne's Milestone 1 Telegram conversation loop."""

import logging

from pydantic import ValidationError
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .codex import CodexConversation
from .config import Settings
from .telegram_bot import AriadneBot

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure Ariadne logs without exposing Telegram request URLs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    """Start the Telegram long-polling application."""
    configure_logging()

    try:
        settings = Settings()
    except ValidationError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error

    conversation = CodexConversation(
        settings.vault,
        settings.codex_turn_settings,
        human=settings.human_name,
    )
    ariadne = AriadneBot(settings.allowed_user_id, conversation)

    async def close_codex(_: object) -> None:
        try:
            await conversation.close()
        except Exception:
            LOGGER.exception("Failed to close Codex client")

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_shutdown(close_codex)
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

    LOGGER.info("Starting Ariadne with The Thread vault %s", settings.vault)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        LOGGER.exception("Telegram polling stopped unexpectedly")
        raise


if __name__ == "__main__":
    main()
