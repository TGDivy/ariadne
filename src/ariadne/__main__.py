"""Run Ariadne's Milestone 1 Telegram conversation loop."""

import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters

from .codex import CodexConversation
from .config import ConfigurationError, Settings
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
        settings = Settings.from_environment()
    except ConfigurationError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error

    conversation = CodexConversation(settings.workspace)
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
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, ariadne.text)
    )

    LOGGER.info("Starting Ariadne with workspace %s", settings.workspace)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        LOGGER.exception("Telegram polling stopped unexpectedly")
        raise


if __name__ == "__main__":
    main()
