"""Telegram adapter for Ariadne's conversation loop."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .codex import CodexConversation

LOGGER = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
STREAM_EDIT_INTERVAL_SECONDS = 1.0
TYPING_REFRESH_INTERVAL_SECONDS = 4.0
PLACEHOLDER_TEXT = "Thinking…"
READY_MESSAGE = "Ariadne is ready."
BUSY_MESSAGE = "I'm still working on your previous message."
FAILURE_MESSAGE = "I ran into a problem while working on that. Please try again."

TypingSender = Callable[[], Awaitable[None]]


def split_for_telegram(text: str) -> list[str]:
    """Split a plain-text response into Telegram-sized pieces."""
    return [
        text[start : start + TELEGRAM_MESSAGE_LIMIT]
        for start in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)
    ]


class AriadneBot:
    """Translate Telegram updates into one shared Codex conversation."""

    def __init__(self, allowed_user_id: int, conversation: CodexConversation) -> None:
        self._allowed_user_id = allowed_user_id
        self._conversation = conversation
        self._busy = False

    async def start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle the sole supported command."""
        message = self._message_from(update)
        if message is None:
            return
        await self.handle_start(message, self._user_id_from(update))

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle a normal Telegram text message."""
        message = self._message_from(update)
        if message is None or message.text is None:
            return

        async def send_typing() -> None:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
                message_thread_id=message.message_thread_id,
            )

        await self.handle_text(
            message,
            self._user_id_from(update),
            message.text,
            send_typing=send_typing,
        )

    async def handle_start(self, message: Message, user_id: int | None) -> None:
        """Respond to an allowed user's /start command."""
        if not self._is_allowed(user_id):
            return
        await self._reply_safely(message, READY_MESSAGE)

    async def handle_text(
        self,
        message: Message,
        user_id: int | None,
        text: str,
        *,
        send_typing: TypingSender | None = None,
    ) -> None:
        """Send one user message through Codex and stream its answer back."""
        if not self._is_allowed(user_id):
            return

        if self._busy:
            await self._reply_safely(message, BUSY_MESSAGE)
            return

        self._busy = True
        typing_task = (
            asyncio.create_task(self._refresh_typing(send_typing))
            if send_typing is not None
            else None
        )
        try:
            placeholder = await self._reply_safely(message, PLACEHOLDER_TEXT)
            if placeholder is None:
                return

            try:
                await self._stream_response(message, placeholder, text)
            except Exception:
                LOGGER.exception("Codex turn failed")
                await self._send_failure(message, placeholder)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_task
            self._busy = False

    async def _refresh_typing(self, send_typing: TypingSender) -> None:
        """Keep Telegram's short-lived typing indicator visible for a turn."""
        while True:
            try:
                await send_typing()
            except TelegramError:
                LOGGER.exception("Telegram typing indicator failed")
            await asyncio.sleep(TYPING_REFRESH_INTERVAL_SECONDS)

    async def _stream_response(
        self, message: Message, placeholder: Message, prompt: str
    ) -> None:
        final_response = ""
        last_edit_at = 0.0
        last_rendered_text = PLACEHOLDER_TEXT

        async for response in self._conversation.stream_reply(prompt):
            final_response = response
            preview = split_for_telegram(response)[0]
            now = time.monotonic()
            if (
                preview != last_rendered_text
                and now - last_edit_at >= STREAM_EDIT_INTERVAL_SECONDS
                and await self._edit_safely(placeholder, preview)
            ):
                last_edit_at = now
                last_rendered_text = preview

        if not final_response:
            raise RuntimeError("Codex completed without an agent response.")

        await self._send_final_response(
            message,
            placeholder,
            final_response,
            last_rendered_text,
        )

    async def _send_final_response(
        self,
        message: Message,
        placeholder: Message,
        response: str,
        last_rendered_text: str,
    ) -> None:
        chunks = split_for_telegram(response)
        first_chunk_is_visible = chunks[0] == last_rendered_text
        if not first_chunk_is_visible:
            first_chunk_is_visible = await self._edit_safely(placeholder, chunks[0])

        if first_chunk_is_visible:
            remaining_chunks = chunks[1:]
        else:
            remaining_chunks = chunks

        for chunk in remaining_chunks:
            await self._reply_safely(message, chunk)

    async def _send_failure(self, message: Message, placeholder: Message) -> None:
        if not await self._edit_safely(placeholder, FAILURE_MESSAGE):
            await self._reply_safely(message, FAILURE_MESSAGE)

    async def _reply_safely(self, message: Message, text: str) -> Message | None:
        try:
            return await message.reply_text(text)
        except TelegramError:
            LOGGER.exception("Telegram reply failed")
            return None

    async def _edit_safely(self, message: Message, text: str) -> bool:
        try:
            await message.edit_text(text)
        except TelegramError:
            LOGGER.exception("Telegram message update failed")
            return False
        return True

    def _is_allowed(self, user_id: int | None) -> bool:
        if user_id == self._allowed_user_id:
            return True
        LOGGER.warning(
            "Ignoring message from unauthorized Telegram user id=%s", user_id
        )
        return False

    @staticmethod
    def _message_from(update: Update) -> Message | None:
        message = update.effective_message
        return message if isinstance(message, Message) else None

    @staticmethod
    def _user_id_from(update: Update) -> int | None:
        user = update.effective_user
        return user.id if user is not None else None
