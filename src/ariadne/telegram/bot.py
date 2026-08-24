"""Telegram adapter for Ariadne's conversation loop."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from telegram import (
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
)
from telegram.constants import ChatAction, FileSizeLimit, ParseMode
from telegram.error import TelegramError, TimedOut
from telegram.ext import ContextTypes

from ..codex import CodexConversation, CodexModel, TurnInterrupted, WebSearchSetting
from .file_delivery import FileDelivery, FileDeliveryError
from .format import split_for_telegram, telegram_messages

LOGGER = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = int(FileSizeLimit.FILESIZE_DOWNLOAD)
ATTACHMENT_ROOT = Path.home() / ".ariadne" / "attachments"
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
DRAFT_INTERVAL_SECONDS = 1.0
TURN_REFRESH_INTERVAL_SECONDS = 4.0
ALBUM_DEBOUNCE_SECONDS = 1.0
READY_MESSAGE = "Ariadne is ready."
NEW_CONVERSATION_MESSAGE = "Started a new conversation. The Thread is still available."
BUSY_MESSAGE = "I'm still working on your previous message."
STEERED_MESSAGE = "Noted — folding that into what I'm working on."
IMAGE_WITHOUT_CAPTION = "Please inspect the attached image."
IMAGES_WITHOUT_CAPTION = "Please inspect the attached images."
DOCUMENT_WITHOUT_CAPTION = "I've sent you a file."
DOCUMENTS_WITHOUT_CAPTION = "I've sent you some files."
DOCUMENT_TOO_LARGE_MESSAGE = (
    "That file is too large; Telegram only lets me download files up to 20 MB."
)
DOCUMENT_FAILED_MESSAGE = "I couldn't download that file. Please try again."
STEERING_FAILED_MESSAGE = (
    "I couldn't add that to the turn I'm working on. Please try again."
)
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


def turn_text(
    text: str,
    message_id: int,
    replied_message: Message | None = None,
) -> str:
    """Add Telegram identity and immediate reply context to one turn."""
    parts: list[str] = []
    if replied_message is not None:
        content = (
            replied_message.text
            if replied_message.text is not None
            else replied_message.caption
        )
        if content is None:
            content = "[The replied-to message has no text or caption.]"
        parts.append(
            f"Telegram reply context (message id {replied_message.message_id}):\n"
            f"<quoted_message>\n{content}\n</quoted_message>"
        )
    parts.extend((text, f"Telegram message id: {message_id}"))
    return "\n\n".join(parts)


def _already_said(response: str, spoken: Sequence[str]) -> bool:
    """Return whether Iris has already sent this exact text herself."""
    normalized = " ".join(response.split())
    return any(" ".join(said.split()) == normalized for said in spoken)


def _document_filename(document: Document) -> str:
    """Return the sender's filename, stripped of any directory component."""
    name = Path(document.file_name or "").name
    return name if name not in {"", ".", ".."} else "document"


def _attachment_name(media: PhotoSize | Document) -> str:
    """Return the filename to keep a downloaded attachment under."""
    if isinstance(media, Document):
        return _document_filename(media)
    return f"photo-{datetime.now():%H%M%S}.jpg"


def attachment_path(name: str) -> Path:
    """Reserve a path for a sent file, under a folder for today.

    Attachments are kept rather than deleted, so the name has to stay readable
    and cannot collide with something sent earlier the same day.
    """
    directory = ATTACHMENT_ROOT / date.today().isoformat()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    attempt = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{attempt}{suffix}"
        attempt += 1
    return candidate


def document_message(
    caption: str | None, documents: Sequence[tuple[Path, str | None]]
) -> str:
    """Compose the turn text for the files the user sent."""
    default = (
        DOCUMENT_WITHOUT_CAPTION if len(documents) == 1 else DOCUMENTS_WITHOUT_CAPTION
    )
    lines = [
        f"Attached file: {path}"
        if mime_type is None
        else f"Attached file: {path} ({mime_type})"
        for path, mime_type in documents
    ]
    return "\n\n".join([caption or default, *lines])


class _Draft:
    """Telegram's ephemeral preview of the reply Iris is still forming.

    A draft leaves no message behind: it animates in place while the turn runs
    and expires by itself, so only what Iris deliberately sends is permanent.
    """

    def __init__(self, message: Message) -> None:
        self._message = message
        self._text: str | None = None
        self._sent_at = 0.0
        self._timed_out = False

    async def show(self, text: str | None) -> None:
        """Show newly streamed text, no faster than Telegram wants to animate."""
        if text == self._text:
            return
        if time.monotonic() - self._sent_at < DRAFT_INTERVAL_SECONDS:
            return
        self._text = text
        await self.keep_alive()

    async def keep_alive(self) -> None:
        """Re-send the preview, which Telegram drops after thirty seconds."""
        self._sent_at = time.monotonic()
        try:
            await self._message.reply_text_draft(self._message.message_id, self._text)
        except TimedOut:
            if not self._timed_out:
                LOGGER.warning("Telegram draft update timed out; will retry.")
            self._timed_out = True
        except TelegramError:
            LOGGER.exception("Telegram draft update failed")
        else:
            self._timed_out = False


@dataclass(frozen=True, slots=True)
class _Attachment:
    """One downloaded file waiting to be sent to Codex."""

    path: Path
    caption: str | None
    mime_type: str | None
    is_image: bool


@dataclass(slots=True)
class _Album:
    """Files from one Telegram message, or one media group, sent together."""

    message: Message
    user_id: int | None
    send_typing: "TypingSender | None" = None
    items: list[_Attachment] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None


class AriadneBot:
    """Translate Telegram updates into one shared Codex conversation."""

    def __init__(
        self,
        allowed_user_id: int,
        conversation: CodexConversation,
        *,
        bot_token: str,
    ) -> None:
        self._allowed_user_id = allowed_user_id
        self._bot_token = bot_token
        self._conversation = conversation
        self._busy = False
        self._stopping = False
        self._stop_notice: Message | None = None
        self._file_delivery = FileDelivery()
        self._albums: dict[str, _Album] = {}

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
        await self._approve_staged_files(
            message, self._user_id_from(update), approval_id, replace_message=True
        )

    async def _approve_staged_files(
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
                token=self._bot_token,
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

        await self._accept_attachment(
            message,
            self._user_id_from(update),
            _Attachment(
                path,
                message.caption,
                image.mime_type if isinstance(image, Document) else None,
                is_image=True,
            ),
            send_typing,
        )

    async def document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Download a document message and hand its local path to Codex."""
        message = self._message_from(update)
        if message is None:
            return
        if not self._is_allowed(self._user_id_from(update)):
            return
        document = message.document
        if document is None:
            return
        if document.file_size is not None and document.file_size > MAX_DOCUMENT_BYTES:
            await self._reply_safely(message, DOCUMENT_TOO_LARGE_MESSAGE)
            return

        async def send_typing() -> None:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
                message_thread_id=message.message_thread_id,
            )

        try:
            path = await self._download_document(context, document)
        except (OSError, TelegramError):
            LOGGER.exception("Document download failed")
            await self._reply_safely(message, DOCUMENT_FAILED_MESSAGE)
            return

        await self._accept_attachment(
            message,
            self._user_id_from(update),
            _Attachment(path, message.caption, document.mime_type, is_image=False),
            send_typing,
        )

    async def handle_document(
        self,
        message: Message,
        user_id: int | None,
        path: Path,
        *,
        caption: str | None = None,
        mime_type: str | None = None,
        send_typing: TypingSender | None = None,
    ) -> None:
        """Send one downloaded file through Codex as an ordinary message."""
        await self._accept_attachment(
            message,
            user_id,
            _Attachment(path, caption, mime_type, is_image=False),
            send_typing,
        )

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
        self._stop_notice = await self._reply_safely(message, STOPPING_MESSAGE)

        try:
            if not await self._conversation.interrupt():
                LOGGER.info("Stop requested before the Codex turn started")
        except Exception:
            self._stopping = False
            self._stop_notice = None
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

        reply_to = (
            message.reply_to_message.message_id
            if message.reply_to_message is not None
            else None
        )
        LOGGER.info(
            "Telegram message received message_id=%s reply_to=%s images=%d",
            message.message_id,
            reply_to,
            len(image_paths),
        )
        prompt = turn_text(text, message.message_id, message.reply_to_message)
        if self._busy:
            LOGGER.info(
                "Telegram message steering active turn message_id=%s",
                message.message_id,
            )
            await self._steer_active_turn(message, prompt, image_paths)
            return

        started_at = time.monotonic()
        status = "cancelled"
        self._busy = True
        LOGGER.info(
            "Telegram turn started message_id=%s model=%s effort=%s",
            message.message_id,
            self._conversation.settings.model,
            self._conversation.settings.effort.value,
        )
        draft = _Draft(message)
        await draft.keep_alive()
        live_turn = asyncio.create_task(self._keep_turn_alive(draft, send_typing))
        try:
            if self._stopping:
                await self._send_stopped(message)
                return

            try:
                await self._stream_response(message, draft, prompt, image_paths)
                status = "success"
            except TurnInterrupted:
                status = "cancelled"
                LOGGER.info(
                    "Telegram turn interrupted message_id=%s", message.message_id
                )
                await self._send_stopped(message)
            except Exception:
                status = "failure"
                LOGGER.exception(
                    "Telegram turn failed message_id=%s", message.message_id
                )
                await self._send_failure(message)
        finally:
            live_turn.cancel()
            with suppress(asyncio.CancelledError):
                await live_turn
            self._stop_notice = None
            self._stopping = False
            self._busy = False
            LOGGER.info(
                "Telegram turn finished message_id=%s status=%s duration=%.2fs",
                message.message_id,
                status,
                time.monotonic() - started_at,
            )

    async def _accept_attachment(
        self,
        message: Message,
        user_id: int | None,
        attachment: _Attachment,
        send_typing: TypingSender | None,
    ) -> None:
        """Send one file to Codex, or hold it for the rest of its media group.

        Telegram delivers an album as one update per file, so a media group is
        gathered behind a short sliding timer and sent as a single turn.
        """
        group = message.media_group_id
        if group is None:
            await self._submit_album(
                _Album(message, user_id, send_typing, [attachment])
            )
            return

        album = self._albums.get(group)
        if album is None:
            album = _Album(message, user_id, send_typing)
            self._albums[group] = album
        album.items.append(attachment)
        if album.timer is not None:
            album.timer.cancel()
        album.timer = asyncio.create_task(self._submit_album_later(group))

    async def _submit_album_later(self, group: str) -> None:
        """Send a media group once it has stopped growing."""
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
            album = self._albums.pop(group, None)
            if album is not None:
                await self._submit_album(album)

    async def _submit_album(self, album: _Album) -> None:
        """Turn one message or media group into a single Codex turn."""
        images = tuple(item.path for item in album.items if item.is_image)
        documents = [
            (item.path, item.mime_type) for item in album.items if not item.is_image
        ]
        caption = next((item.caption for item in album.items if item.caption), None)

        if documents:
            text = document_message(caption, documents)
        elif caption:
            text = caption
        else:
            text = IMAGE_WITHOUT_CAPTION if len(images) == 1 else IMAGES_WITHOUT_CAPTION

        await self.handle_text(
            album.message,
            album.user_id,
            text,
            image_paths=images,
            send_typing=album.send_typing,
        )

    async def _steer_active_turn(
        self,
        message: Message,
        text: str,
        image_paths: tuple[Path, ...],
    ) -> None:
        """Feed a follow-up message into the turn Ariadne is already running."""
        try:
            steered = await self._conversation.steer(text, image_paths=image_paths)
        except Exception:
            LOGGER.exception("Codex turn steering failed")
            await self._reply_safely(message, STEERING_FAILED_MESSAGE)
            return

        if steered:
            LOGGER.info("Telegram steering accepted message_id=%s", message.message_id)
            await self._reply_safely(message, STEERED_MESSAGE)
            return

        # The turn is starting but Codex has not accepted it yet.
        LOGGER.info(
            "Telegram steering deferred message_id=%s turn_not_ready=true",
            message.message_id,
        )
        await self._reply_safely(message, BUSY_MESSAGE)

    async def _keep_turn_alive(
        self, draft: _Draft, send_typing: TypingSender | None
    ) -> None:
        """Hold the typing indicator and the ephemeral draft open for a turn."""
        while True:
            if send_typing is not None:
                try:
                    await send_typing()
                except TimedOut:
                    LOGGER.warning("Telegram typing indicator timed out; will retry.")
                except TelegramError:
                    LOGGER.exception("Telegram typing indicator failed")
            await asyncio.sleep(TURN_REFRESH_INTERVAL_SECONDS)
            await draft.keep_alive()

    async def _stream_response(
        self,
        message: Message,
        draft: _Draft,
        prompt: str,
        image_paths: tuple[Path, ...] = (),
    ) -> None:
        final_response = ""
        spoken: list[str] = []

        async def show_activity(activity: str) -> None:
            if final_response or self._stopping:
                return
            await draft.show(activity)

        async for response in self._conversation.stream_reply(
            prompt,
            image_paths=image_paths,
            activity=show_activity,
            spoken=spoken.append,
            stop_requested=lambda: self._stopping,
        ):
            final_response = response
            if not self._stopping:
                await draft.show(split_for_telegram(response)[0])

        if not final_response:
            raise RuntimeError("Codex completed without an agent response.")

        if _already_said(final_response, spoken):
            LOGGER.info("Iris already sent her answer herself; not repeating it")
            return

        await self._send_final_response(message, final_response)

    async def _send_final_response(self, message: Message, response: str) -> None:
        """Persist Iris's answer, the way she would have sent it herself."""
        messages, is_html = telegram_messages(response)
        if is_html:
            sent = await self._reply_safely(
                message, messages[0], parse_mode=ParseMode.HTML
            )
            if sent is not None:
                return
            messages = split_for_telegram(response)

        for chunk in messages:
            await self._reply_safely(message, chunk)

    async def _send_failure(self, message: Message) -> None:
        await self._reply_safely(message, FAILURE_MESSAGE)

    async def _send_stopped(self, message: Message) -> None:
        notice = self._stop_notice
        if notice is not None and await self._edit_safely(notice, STOPPED_MESSAGE):
            return
        await self._reply_safely(message, STOPPED_MESSAGE)

    async def _reply_safely(
        self,
        message: Message,
        text: str,
        *,
        parse_mode: ParseMode | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        try:
            return await message.reply_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
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
        return await self._download(context, image, MAX_IMAGE_BYTES)

    async def _download_document(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        document: Document,
    ) -> Path:
        return await self._download(context, document, MAX_DOCUMENT_BYTES)

    @staticmethod
    async def _download(
        context: ContextTypes.DEFAULT_TYPE,
        media: PhotoSize | Document,
        size_limit: int,
    ) -> Path:
        """Save one sent file into the attachment archive."""
        path = attachment_path(_attachment_name(media))
        try:
            telegram_file = await context.bot.get_file(media.file_id)
            await telegram_file.download_to_drive(custom_path=path)
            if path.stat().st_size > size_limit:
                raise OSError("Downloaded file exceeds size limit")
            return path
        except (OSError, TelegramError):
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _user_id_from(update: Update) -> int | None:
        user = update.effective_user
        return user.id if user is not None else None
