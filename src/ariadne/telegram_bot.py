"""Telegram adapter for Ariadne's conversation loop."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from telegram import Document, Message, PhotoSize, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError, TimedOut
from telegram.ext import ContextTypes

from .codex import CodexConversation
from .telegram_format import render_telegram_html

LOGGER = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_ATTACHMENT_DIRNAME = ".ariadne-attachments"
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
STREAM_EDIT_INTERVAL_SECONDS = 1.0
TYPING_REFRESH_INTERVAL_SECONDS = 4.0
PLACEHOLDER_TEXT = "Thinking…"
READY_MESSAGE = "Ariadne is ready."
NEW_CONVERSATION_MESSAGE = "Started a new conversation. The Thread is still available."
BUSY_MESSAGE = "I'm still working on your previous message."
FAILURE_MESSAGE = "I ran into a problem while working on that. Please try again."

TypingSender = Callable[[], Awaitable[None]]


def split_for_telegram(text: str) -> list[str]:
    """Split plain text at readable boundaries within Telegram's message limit."""
    chunks: list[str] = []
    remaining = text

    while len(remaining) > TELEGRAM_MESSAGE_LIMIT:
        split_at = _telegram_split_point(remaining)
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]

    if remaining:
        chunks.append(remaining)
    return chunks


def _telegram_split_point(text: str) -> int:
    """Find a readable boundary without creating a tiny first message."""
    for separator in ("\n\n", "\n", " "):
        split_at = text.rfind(separator, 0, TELEGRAM_MESSAGE_LIMIT)
        if split_at >= TELEGRAM_MESSAGE_LIMIT // 2:
            return split_at + len(separator)
    return TELEGRAM_MESSAGE_LIMIT


class AriadneBot:
    """Translate Telegram updates into one shared Codex conversation."""

    def __init__(self, allowed_user_id: int, conversation: CodexConversation) -> None:
        self._allowed_user_id = allowed_user_id
        self._conversation = conversation
        self._busy = False

    async def start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start."""
        message = self._message_from(update)
        if message is None:
            return
        await self.handle_start(message, self._user_id_from(update))

    async def new(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /new."""
        message = self._message_from(update)
        if message is None:
            return
        await self.handle_new(message, self._user_id_from(update))

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

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Download an image message and send it to Codex with its caption."""
        message = self._message_from(update)
        if message is None:
            return
        if not self._is_allowed(self._user_id_from(update)):
            return
        if self._busy:
            await self._reply_safely(message, BUSY_MESSAGE)
            return
        image = self._image_from(message)
        if image is None:
            return
        if (
            isinstance(image, Document)
            and image.mime_type not in SUPPORTED_IMAGE_MIME_TYPES
        ):
            await self._reply_safely(
                message,
                "I support JPEG, PNG, and WebP images. "
                "Please convert this image and try again.",
            )
            return
        if image.file_size is not None and image.file_size > MAX_IMAGE_BYTES:
            await self._reply_safely(
                message, "That image is too large; the limit is 10 MB."
            )
            return

        async def send_typing() -> None:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
                message_thread_id=message.message_thread_id,
            )

        try:
            path = await self._download_image(message, context, image)
        except (OSError, TelegramError):
            LOGGER.exception("Image download failed")
            await self._reply_safely(
                message, "I couldn't download that image. Please try again."
            )
            return

        try:
            await self.handle_text(
                message,
                self._user_id_from(update),
                message.caption or "Please inspect the attached image.",
                image_paths=(path,),
                send_typing=send_typing,
            )
        finally:
            path.unlink(missing_ok=True)

    async def handle_start(self, message: Message, user_id: int | None) -> None:
        """Respond to an allowed user's /start command."""
        if not self._is_allowed(user_id):
            return
        await self._reply_safely(message, READY_MESSAGE)

    async def handle_new(self, message: Message, user_id: int | None) -> None:
        """Start a fresh Codex session without changing The Thread."""
        if not self._is_allowed(user_id):
            return
        if self._busy:
            await self._reply_safely(message, BUSY_MESSAGE)
            return

        self._conversation.reset()
        await self._reply_safely(message, NEW_CONVERSATION_MESSAGE)

    async def handle_text(
        self,
        message: Message,
        user_id: int | None,
        text: str,
        image_paths: tuple[Path, ...] = (),
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
                await self._stream_response(message, placeholder, text, image_paths)
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
            except TimedOut:
                LOGGER.warning("Telegram typing indicator timed out; will retry.")
            except TelegramError:
                LOGGER.exception("Telegram typing indicator failed")
            await asyncio.sleep(TYPING_REFRESH_INTERVAL_SECONDS)

    async def _stream_response(
        self,
        message: Message,
        placeholder: Message,
        prompt: str,
        image_paths: tuple[Path, ...] = (),
    ) -> None:
        final_response = ""
        last_edit_at = 0.0
        last_rendered_text = PLACEHOLDER_TEXT

        responses = (
            self._conversation.stream_reply(prompt, image_paths=image_paths)
            if image_paths
            else self._conversation.stream_reply(prompt)
        )
        async for response in responses:
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
        try:
            formatted_response = render_telegram_html(response)
        except Exception:
            LOGGER.exception("Telegram response formatting failed")
        else:
            if formatted_response and len(formatted_response) <= TELEGRAM_MESSAGE_LIMIT:
                if formatted_response == last_rendered_text:
                    return
                if await self._edit_safely(
                    placeholder,
                    formatted_response,
                    parse_mode=ParseMode.HTML,
                ):
                    return
                await self._reply_safely(message, response)
                return

        await self._send_plain_final_response(
            message,
            placeholder,
            response,
            last_rendered_text,
        )

    async def _send_plain_final_response(
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

    async def _edit_safely(
        self,
        message: Message,
        text: str,
        *,
        parse_mode: ParseMode | None = None,
    ) -> bool:
        try:
            await message.edit_text(text, parse_mode=parse_mode)
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
    def _image_from(message: Message) -> PhotoSize | Document | None:
        if message.photo:
            return message.photo[-1]
        if message.document and message.document.mime_type is not None:
            if message.document.mime_type.startswith("image/"):
                return message.document
        return None

    async def _download_image(
        self,
        message: Message,
        context: ContextTypes.DEFAULT_TYPE,
        image: PhotoSize | Document,
    ) -> Path:
        attachment_dir = Path.cwd() / IMAGE_ATTACHMENT_DIRNAME
        attachment_dir.mkdir(mode=0o700, exist_ok=True)
        suffix = ".jpg"
        if isinstance(image, Document) and image.file_name:
            candidate = Path(image.file_name).suffix.lower()
            if candidate in {".jpg", ".jpeg", ".png", ".webp"}:
                suffix = candidate
        path = attachment_dir / f"{uuid4().hex}{suffix}"
        try:
            telegram_file = await context.bot.get_file(image.file_id)
            await telegram_file.download_to_drive(custom_path=path)
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise OSError("Downloaded image exceeds size limit")
            return path
        except (OSError, TelegramError):
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _user_id_from(update: Update) -> int | None:
        user = update.effective_user
        return user.id if user is not None else None
