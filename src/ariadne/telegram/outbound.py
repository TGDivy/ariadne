"""Proactive Telegram delivery used by non-Telegram turn profiles."""

from telegram import Bot

from .rich import RichBotAPI, split_rich_markdown


async def send_rich_text(token: str, chat_id: int, markdown: str) -> list[int]:
    """Send complete Rich Markdown chunks as top-level Telegram messages."""
    message_ids: list[int] = []
    async with Bot(token) as bot:
        api = RichBotAPI(bot)
        for chunk in split_rich_markdown(markdown):
            message = await api.send(chat_id=chat_id, markdown=chunk)
            message_ids.append(message.message_id)
    return message_ids
