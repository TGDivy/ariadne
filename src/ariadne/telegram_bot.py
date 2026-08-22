"""Telegram adapter for Ariadne's conversation loop."""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from telegram import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError, TimedOut
from telegram.ext import ContextTypes

from .codex import CodexConversation, CodexModel, TurnInterrupted, WebSearchSetting
from .file_delivery import FileDelivery, FileDeliveryError
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
STOPPING_MESSAGE = "Stopping…"
STOPPED_MESSAGE = "Stopped."
NOTHING_TO_STOP_MESSAGE = "There isn't an active turn to stop."
SETTINGS_UNAVAILABLE_MESSAGE = (
    "I couldn't load the available Codex settings. Please try again."
)
SETTINGS_BUSY_MESSAGE = "Settings can't change while Ariadne is working."

SETTINGS_CALLBACK_PREFIX = "settings:"
SETTINGS_MODELS_CALLBACK = "settings:models"
SETTINGS_EFFORT_CALLBACK = "settings:effort"
SETTINGS_WEB_CALLBACK = "settings:web"
SETTINGS_BACK_CALLBACK = "settings:back"

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
        self._stopping = False
        self._active_placeholder: Message | None = None
        self._file_delivery = FileDelivery()

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

    async def stop(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop."""
        message = self._message_from(update)
        if message is None:
            return
        await self.handle_stop(message, self._user_id_from(update))

    async def approve(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Deliver a file batch only after an explicit Telegram command."""
        message = self._message_from(update)
        if message is None or not self._is_allowed(self._user_id_from(update)):
            return
        arguments = (message.text or "").split(maxsplit=1)
        if len(arguments) != 2:
            await self._reply_safely(message, "Usage: /approve <file-delivery-id>")
            return
        await self.handle_approve(message, self._user_id_from(update), arguments[1])

    async def file_delivery_callback(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Apply an explicit Telegram file-delivery button selection."""
        query = update.callback_query
        if not isinstance(query, CallbackQuery):
            return
        await self._answer_callback_safely(query)
        message = query.message
        if not isinstance(message, Message) or not isinstance(query.data, str):
            return
        if not self._is_allowed(self._user_id_from(update)):
            return
        parts = query.data.split(":", maxsplit=2)
        if len(parts) != 3:
            return
        _prefix, action, approval_id = parts
        if action == "reject":
            self._file_delivery.reject(approval_id)
            await self._edit_safely(message, "File delivery cancelled.")
            return
        if action != "approve":
            return
        await self.handle_approve(
            message, self._user_id_from(update), approval_id, replace_message=True
        )

    async def handle_approve(
        self,
        message: Message,
        user_id: int | None,
        approval_id: str,
        *,
        replace_message: bool = False,
    ) -> None:
        """Deliver one staged batch after validating the Telegram user."""
        if not self._is_allowed(user_id):
            return
        try:
            files = await self._file_delivery.approve(
                approval_id,
                token=os.environ["TELEGRAM_BOT_TOKEN"],
                chat_id=message.chat_id,
            )
        except (FileDeliveryError, KeyError):
            await self._delivery_result(
                message, "I couldn't deliver that staged file batch.", replace_message
            )
            return
        noun = "file" if len(files) == 1 else "files"
        await self._delivery_result(
            message, f"Sent {len(files)} {noun}.", replace_message
        )

    async def _delivery_result(
        self, message: Message, text: str, replace_message: bool
    ) -> None:
        if replace_message and await self._edit_safely(message, text):
            return
        await self._reply_safely(message, text)

    async def settings(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /settings."""
        message = self._message_from(update)
        if message is None:
            return
        await self.handle_settings(message, self._user_id_from(update))

    async def settings_callback(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle a button press from Ariadne's settings panel."""
        query = update.callback_query
        if not isinstance(query, CallbackQuery):
            return

        await self._answer_callback_safely(query)
        if not self._is_allowed(self._user_id_from(update)):
            return

        message = query.message
        if not isinstance(message, Message) or not isinstance(query.data, str):
            return
        await self.handle_settings_callback(
            message,
            self._user_id_from(update),
            query.data,
        )

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

    async def handle_stop(self, message: Message, user_id: int | None) -> None:
        """Request that Codex stop the one active Ariadne turn."""
        if not self._is_allowed(user_id):
            return
        if not self._busy:
            await self._reply_safely(message, NOTHING_TO_STOP_MESSAGE)
            return
        if self._stopping:
            await self._reply_safely(message, STOPPING_MESSAGE)
            return

        self._stopping = True
        placeholder = self._active_placeholder
        if placeholder is not None:
            await self._edit_safely(placeholder, STOPPING_MESSAGE)
        else:
            await self._reply_safely(message, STOPPING_MESSAGE)

        try:
            if not await self._conversation.interrupt():
                LOGGER.info("Stop requested before the Codex turn started")
        except Exception:
            self._stopping = False
            LOGGER.exception("Codex turn interruption failed")
            await self._reply_safely(
                message,
                "I couldn't stop the active turn. Please try again.",
            )

    async def handle_settings(self, message: Message, user_id: int | None) -> None:
        """Show the process-local Codex settings panel."""
        if not self._is_allowed(user_id):
            return
        await self._reply_safely(
            message,
            self._settings_text(),
            reply_markup=self._settings_keyboard(),
        )

    async def handle_settings_callback(
        self,
        message: Message,
        user_id: int | None,
        data: str,
    ) -> None:
        """Apply a validated settings-panel selection."""
        if not self._is_allowed(user_id) or not data.startswith(
            SETTINGS_CALLBACK_PREFIX
        ):
            return

        if data == SETTINGS_BACK_CALLBACK:
            await self._show_settings(message)
        elif data == SETTINGS_MODELS_CALLBACK:
            await self._show_model_choices(message)
        elif data == SETTINGS_EFFORT_CALLBACK:
            await self._show_effort_choices(message)
        elif data == SETTINGS_WEB_CALLBACK:
            await self._show_web_choices(message)
        elif data.startswith("settings:model:"):
            await self._select_model(message, data.removeprefix("settings:model:"))
        elif data.startswith("settings:effort:"):
            await self._select_effort(message, data.removeprefix("settings:effort:"))
        elif data.startswith("settings:web:"):
            await self._select_web_mode(message, data.removeprefix("settings:web:"))

    async def _show_settings(self, message: Message) -> None:
        await self._edit_safely(
            message,
            self._settings_text(),
            reply_markup=self._settings_keyboard(),
        )

    async def _show_model_choices(self, message: Message) -> None:
        models = await self._available_models(message)
        if models is None:
            return

        await self._edit_safely(
            message,
            "Choose a Codex model.",
            reply_markup=self._models_keyboard(models),
        )

    async def _show_effort_choices(self, message: Message) -> None:
        model = await self._current_model(message)
        if model is None:
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    self._effort_button_text(effort.value),
                    callback_data=f"settings:effort:{effort.value}",
                )
            ]
            for effort in model.supported_efforts
        ]
        keyboard.append(
            [InlineKeyboardButton("Back", callback_data=SETTINGS_BACK_CALLBACK)]
        )
        await self._edit_safely(
            message,
            f"Choose reasoning effort for {model.display_name}.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _show_web_choices(self, message: Message) -> None:
        settings = self._conversation.settings
        keyboard = [
            [
                InlineKeyboardButton(
                    self._selected_button_text(
                        "Off", settings.web_search == "disabled"
                    ),
                    callback_data="settings:web:disabled",
                ),
                InlineKeyboardButton(
                    self._selected_button_text("Live", settings.web_search == "live"),
                    callback_data="settings:web:live",
                ),
            ],
            [InlineKeyboardButton("Back", callback_data=SETTINGS_BACK_CALLBACK)],
        ]
        await self._edit_safely(
            message,
            "Choose web research mode.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _select_model(self, message: Message, identifier: str) -> None:
        if not await self._settings_can_change(message):
            return
        models = await self._available_models(message)
        if models is None:
            return
        model = next(
            (model for model in models if model.identifier == identifier), None
        )
        if model is None:
            await self._show_settings(message)
            return

        settings = self._conversation.settings
        effort = (
            settings.effort
            if settings.effort in model.supported_efforts
            else model.default_effort
        )
        self._conversation.set_settings(
            replace(settings, model=model.identifier, effort=effort)
        )
        await self._show_settings(message)

    async def _select_effort(self, message: Message, value: str) -> None:
        if not await self._settings_can_change(message):
            return
        model = await self._current_model(message)
        if model is None:
            return
        effort = next(
            (effort for effort in model.supported_efforts if effort.value == value),
            None,
        )
        if effort is None:
            await self._show_effort_choices(message)
            return

        self._conversation.set_settings(
            replace(self._conversation.settings, effort=effort)
        )
        await self._show_settings(message)

    async def _select_web_mode(self, message: Message, value: str) -> None:
        if not await self._settings_can_change(message):
            return
        if value == "disabled":
            web_search: WebSearchSetting = "disabled"
        elif value == "live":
            web_search = "live"
        else:
            await self._show_web_choices(message)
            return

        self._conversation.set_settings(
            replace(self._conversation.settings, web_search=web_search)
        )
        await self._show_settings(message)

    async def _settings_can_change(self, message: Message) -> bool:
        if not self._busy:
            return True
        await self._edit_safely(message, SETTINGS_BUSY_MESSAGE)
        return False

    async def _available_models(
        self, message: Message
    ) -> tuple[CodexModel, ...] | None:
        try:
            models = await self._conversation.available_models()
        except Exception:
            LOGGER.exception("Codex model list failed")
            await self._edit_safely(message, SETTINGS_UNAVAILABLE_MESSAGE)
            return None
        if models:
            return models

        LOGGER.error("Codex model list returned no selectable models")
        await self._edit_safely(message, SETTINGS_UNAVAILABLE_MESSAGE)
        return None

    async def _current_model(self, message: Message) -> CodexModel | None:
        models = await self._available_models(message)
        if models is None:
            return None
        model = next(
            (
                model
                for model in models
                if model.identifier == self._conversation.settings.model
            ),
            None,
        )
        if model is not None:
            return model

        await self._edit_safely(
            message,
            "The selected model is no longer available. Choose a new model.",
            reply_markup=self._models_keyboard(models),
        )
        return None

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
        placeholder: Message | None = None
        try:
            placeholder = await self._reply_safely(message, PLACEHOLDER_TEXT)
            if placeholder is None:
                return
            self._active_placeholder = placeholder

            if self._stopping:
                await self._send_stopped(message, placeholder)
                return

            try:
                await self._stream_response(message, placeholder, text, image_paths)
            except TurnInterrupted:
                LOGGER.info("Codex turn interrupted")
                await self._send_stopped(message, placeholder)
            except Exception:
                LOGGER.exception("Codex turn failed")
                await self._send_failure(message, placeholder)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                with suppress(asyncio.CancelledError):
                    await typing_task
            if placeholder is not None and self._active_placeholder is placeholder:
                self._active_placeholder = None
            self._stopping = False
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

        async def show_activity(activity: str) -> None:
            nonlocal last_edit_at, last_rendered_text
            if final_response or self._stopping:
                return
            now = time.monotonic()
            if (
                activity != last_rendered_text
                and now - last_edit_at >= STREAM_EDIT_INTERVAL_SECONDS
                and await self._edit_safely(placeholder, activity)
            ):
                last_edit_at = now
                last_rendered_text = activity

        async for response in self._conversation.stream_reply(
            prompt,
            image_paths=image_paths,
            activity=show_activity,
            stop_requested=lambda: self._stopping,
        ):
            final_response = response
            if self._stopping:
                continue
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

    async def _send_stopped(self, message: Message, placeholder: Message) -> None:
        if not await self._edit_safely(placeholder, STOPPED_MESSAGE):
            await self._reply_safely(message, STOPPED_MESSAGE)

    async def _reply_safely(
        self,
        message: Message,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        try:
            return await message.reply_text(text, reply_markup=reply_markup)
        except TelegramError:
            LOGGER.exception("Telegram reply failed")
            return None

    async def _edit_safely(
        self,
        message: Message,
        text: str,
        *,
        parse_mode: ParseMode | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        try:
            await message.edit_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except TelegramError:
            LOGGER.exception("Telegram message update failed")
            return False
        return True

    async def _answer_callback_safely(self, query: CallbackQuery) -> None:
        try:
            await query.answer()
        except TelegramError:
            LOGGER.exception("Telegram settings callback acknowledgement failed")

    def _settings_text(self) -> str:
        settings = self._conversation.settings
        web_search = "Live" if settings.web_search == "live" else "Off"
        return (
            "Ariadne settings\n\n"
            f"Model: {settings.model}\n"
            f"Reasoning: {settings.effort.value}\n"
            f"Web research: {web_search}\n\n"
            "Changes start a new in-memory Codex conversation."
        )

    def _settings_keyboard(self) -> InlineKeyboardMarkup:
        settings = self._conversation.settings
        web_search = "Live" if settings.web_search == "live" else "Off"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Model: {settings.model}",
                        callback_data=SETTINGS_MODELS_CALLBACK,
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Reasoning: {settings.effort.value}",
                        callback_data=SETTINGS_EFFORT_CALLBACK,
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Web research: {web_search}",
                        callback_data=SETTINGS_WEB_CALLBACK,
                    )
                ],
            ]
        )

    def _models_keyboard(self, models: tuple[CodexModel, ...]) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton(
                    self._model_button_text(model),
                    callback_data=f"settings:model:{model.identifier}",
                )
            ]
            for model in models
        ]
        keyboard.append(
            [InlineKeyboardButton("Back", callback_data=SETTINGS_BACK_CALLBACK)]
        )
        return InlineKeyboardMarkup(keyboard)

    def _model_button_text(self, model: CodexModel) -> str:
        return self._selected_button_text(
            model.display_name,
            self._conversation.settings.model == model.identifier,
        )

    @staticmethod
    def _effort_button_text(effort: str) -> str:
        return effort.capitalize()

    @staticmethod
    def _selected_button_text(text: str, selected: bool) -> str:
        return f"✓ {text}" if selected else text

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
