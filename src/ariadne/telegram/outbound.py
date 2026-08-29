"""Proactive Telegram delivery used by non-Telegram turn profiles."""

import sqlite3

from telegram import Bot

from .history import (
    TelegramHistoryMessage,
    TelegramMessageSource,
    TelegramMessageStore,
    telegram_message_time,
)
from .rich import RichBotAPI, split_rich_markdown


class TelegramDeliveredWithoutHistoryError(RuntimeError):
    """Telegram accepted messages that local durable history could not record."""

    def __init__(self, message_ids: tuple[int, ...]) -> None:
        self.message_ids = message_ids
        super().__init__("Telegram delivery succeeded but history recording failed.")


async def send_rich_text(
    token: str,
    chat_id: int,
    markdown: str,
    *,
    history: TelegramMessageStore,
    source: TelegramMessageSource,
) -> list[int]:
    """Send complete Rich Markdown chunks as top-level Telegram messages."""
    message_ids: list[int] = []
    history.initialize()
    async with Bot(token) as bot:
        api = RichBotAPI(bot)
        for chunk in split_rich_markdown(markdown):
            message = await api.send(chat_id=chat_id, markdown=chunk)
            message_ids.append(message.message_id)
            try:
                history.record(
                    TelegramHistoryMessage(
                        chat_id=chat_id,
                        message_id=message.message_id,
                        sent_at=telegram_message_time(message),
                        speaker="iris",
                        source=source,
                        content_type="text",
                        text=chunk,
                    )
                )
            except (OSError, sqlite3.Error, ValueError) as error:
                raise TelegramDeliveredWithoutHistoryError(
                    tuple(message_ids)
                ) from error
    return message_ids
