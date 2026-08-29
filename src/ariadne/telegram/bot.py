"""Telegram adapter for Ariadne's conversation loop."""

import asyncio
import logging
import sqlite3
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from telegram import (
    Bot,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
)
from telegram.constants import FileSizeLimit, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..codex import CodexConversation, CodexModel, TurnInterrupted, WebSearchSetting
from ..prompts.activations import (
    EMPTY_TELEGRAM_REPLY,
    build_document_turn_prompt,
    build_image_turn_prompt,
    build_telegram_turn_prompt,
)
from .file_delivery import FileDelivery, FileDeliveryError
from .history import (
    TelegramContentType,
    TelegramHistoryMessage,
    TelegramMessageStore,
    telegram_message_time,
)
from .live import (
    STOPPED_MESSAGE,
    STOPPING_MESSAGE,
    TURN_STOP_CALLBACK,
    LiveTurn,
)
from .questions import (
    QuestionSelection,
    TelegramQuestion,
    TelegramQuestionCard,
    TelegramQuestionStore,
    default_question_state_path,
    parse_question_callback,
)
from .rich import RichBotAPI, incoming_rich_markdown

LOGGER = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = int(FileSizeLimit.FILESIZE_DOWNLOAD)
ATTACHMENT_ROOT = Path.home() / ".ariadne" / "attachments"
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALBUM_DEBOUNCE_SECONDS = 1.0
READY_MESSAGE = "Ariadne is ready."
NEW_CONVERSATION_MESSAGE = (
    "Started a new conversation. Your shared memory is still available."
)
BUSY_MESSAGE = "I'm still working on your previous message."
DOCUMENT_TOO_LARGE_MESSAGE = (
    "That file is too large; Telegram only lets me download files up to 20 MB."
)
DOCUMENT_FAILED_MESSAGE = "I couldn't download that file. Please try again."
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


def turn_text(
    text: str,
    replied_message: Message | None = None,
) -> str:
    """Extract Telegram reply content for the shared activation builder."""
    quoted_message: str | None = None
    if replied_message is not None:
        quoted_message = (
            replied_message.text
            if replied_message.text is not None
            else replied_message.caption
        )
        if quoted_message is None:
            quoted_message = incoming_rich_markdown(replied_message)
        if quoted_message is None:
            quoted_message = EMPTY_TELEGRAM_REPLY
    return build_telegram_turn_prompt(text, quoted_message=quoted_message)


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
    while True:
        try:
            # Telegram can deliver the files in an album concurrently.  The
            # empty file is a reservation: checking `exists()` and creating it
            # later leaves a race in which every download picks the same path.
            candidate.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            candidate = directory / f"{stem}-{attempt}{suffix}"
            attempt += 1
        else:
            return candidate


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
    items: list[_Attachment] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None


def _album_history_text(album: _Album, caption: str | None) -> str:
    """Describe visible media without retaining model-only local paths."""
    markers = [
        "[Photo]" if item.is_image else f"[Document: {item.path.name}]"
        for item in album.items
    ]
    return "\n\n".join([part for part in (caption, *markers) if part])


@dataclass(slots=True)
class _PendingMessage:
    """One accepted Telegram input waiting to steer or start a Codex turn."""

    message: Message
    user_id: int | None
    text: str
    image_paths: tuple[Path, ...]
    force_next_turn: bool = False


class AriadneBot:
    """Translate Telegram updates into one shared Codex conversation."""

    def __init__(
        self,
        allowed_user_id: int,
        conversation: CodexConversation,
        *,
        bot_token: str,
        question_state: Path | None = None,
    ) -> None:
        self._allowed_user_id = allowed_user_id
        self._bot_token = bot_token
        self._conversation = conversation
        self._busy = False
        self._stopping = False
        self._stop_notice: Message | None = None
        self._live_response: LiveTurn | None = None
        self._bot: Bot | None = None
        self._rich_api: RichBotAPI | None = None
        state_path = question_state or default_question_state_path()
        self._questions = TelegramQuestionStore(state_path)
        self._history = TelegramMessageStore(state_path)
        self._history.initialize()
        self._file_delivery = FileDelivery()
        self._albums: dict[str, _Album] = {}
        self._pending_messages: deque[_PendingMessage] = deque()
        self._pending_task: asyncio.Task[None] | None = None
        self._steer_lock = asyncio.Lock()

    def bind_bot(self, bot: Bot) -> None:
        """Bind PTB's initialized bot to the Bot API 10.3 compatibility layer."""
        self._bot = bot
        self._rich_api = RichBotAPI(bot)

    async def recover_questions(self) -> None:
        """Disable question cards orphaned by a previous process lifetime."""
        try:
            questions = self._questions.cancel_pending(self._allowed_user_id)
        except (OSError, sqlite3.Error):
            LOGGER.exception("Telegram question recovery failed")
            return
        for question in questions:
            await self._settle_question(question)

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

    async def turn_callback(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle controls embedded in the currently edited Rich Message."""
        query = update.callback_query
        if not isinstance(query, CallbackQuery):
            return
        await self._answer_callback_safely(query)
        if not self._is_allowed(self._user_id_from(update)):
            return
        message = query.message
        if (
            not isinstance(message, Message)
            or query.data != TURN_STOP_CALLBACK
            or self._live_response is None
            or self._live_response.message_id != message.message_id
        ):
            return
        await self._request_stop(None)

    async def question_callback(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Apply a trusted choice from a live interactive question."""
        query = update.callback_query
        if not isinstance(query, CallbackQuery):
            return
        message = query.message
        if not isinstance(message, Message) or not isinstance(query.data, str):
            await self._answer_callback_safely(
                query, "This question is no longer active."
            )
            return

        selection = self.handle_question_selection(
            message,
            self._user_id_from(update),
            query.data,
        )
        notices = {
            "accepted": "Answer received.",
            "already_answered": "This question was already answered.",
            "inactive": "This question is no longer active.",
            "stale": "This question is no longer active.",
        }
        await self._answer_callback_safely(query, notices[selection.outcome])
        if selection.outcome == "accepted" and selection.question is not None:
            LOGGER.info(
                "Telegram question answered question_id=%s source=button",
                selection.question.question_id,
            )
            await self._settle_question(selection.question)

    def handle_question_selection(
        self,
        message: Message,
        user_id: int | None,
        data: str,
    ) -> QuestionSelection:
        """Validate a callback against the active turn and durable question."""
        if not self._is_allowed(user_id) or not self._busy:
            return QuestionSelection("stale")
        parsed = parse_question_callback(data)
        if parsed is None:
            return QuestionSelection("stale")
        question_id, choice_index = parsed
        try:
            return self._questions.answer_choice(
                question_id,
                chat_id=message.chat_id,
                message_id=message.message_id,
                choice_index=choice_index,
            )
        except (OSError, sqlite3.Error):
            LOGGER.exception("Telegram question callback state failed")
            return QuestionSelection("stale")

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
        await self.handle_text(
            message,
            self._user_id_from(update),
            message.text,
        )

    async def rich_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle a Rich Message that PTB 22.8 preserves as raw Bot API data."""
        # Do not use effective_message here: callback-query updates point at the
        # bot's own Rich Message and must never be fed back as user input.
        message = update.message
        if not isinstance(message, Message) or message.text is not None:
            return
        content = incoming_rich_markdown(message)
        if content is None:
            return

        await self.handle_text(
            message,
            self._user_id_from(update),
            content,
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
        )

    async def handle_document(
        self,
        message: Message,
        user_id: int | None,
        path: Path,
        *,
        caption: str | None = None,
        mime_type: str | None = None,
    ) -> None:
        """Send one downloaded file through Codex as an ordinary message."""
        await self._accept_attachment(
            message,
            user_id,
            _Attachment(path, caption, mime_type, is_image=False),
        )

    async def handle_start(self, message: Message, user_id: int | None) -> None:
        """Respond to an allowed user's /start command."""
        if not self._is_allowed(user_id):
            return
        await self._reply_safely(message, READY_MESSAGE)

    async def handle_new(self, message: Message, user_id: int | None) -> None:
        """Start a fresh Codex session without changing private knowledge."""
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
        await self._request_stop(message)

    async def _request_stop(self, command_message: Message | None) -> None:
        """Interrupt the active turn from either a command or embedded button."""
        if not self._busy:
            if command_message is not None:
                await self._reply_safely(command_message, NOTHING_TO_STOP_MESSAGE)
            return
        if self._stopping:
            if command_message is not None and self._live_response is None:
                await self._reply_safely(command_message, STOPPING_MESSAGE)
            return

        self._stopping = True
        await self._cancel_pending_question()
        if self._live_response is not None:
            await self._live_response.stopping()
        elif command_message is not None:
            self._stop_notice = await self._reply_safely(
                command_message, STOPPING_MESSAGE
            )

        try:
            if not await self._conversation.interrupt():
                LOGGER.info("Stop requested before the Codex turn started")
        except Exception:
            LOGGER.exception("Codex turn interruption failed")
            if self._live_response is not None:
                await self._live_response.resume()
            self._stopping = False
            self._stop_notice = None
            if command_message is not None:
                await self._reply_safely(
                    command_message,
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
        history_text: str | None = None,
        content_type: TelegramContentType = "text",
        record_history: bool = True,
    ) -> None:
        """Send one user message through Codex and stream its answer back."""
        if not self._is_allowed(user_id):
            return

        reply_to = (
            message.reply_to_message.message_id
            if message.reply_to_message is not None
            else None
        )
        if record_history:
            self._history.record(
                TelegramHistoryMessage(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    sent_at=telegram_message_time(message),
                    speaker="human",
                    source="telegram",
                    content_type=content_type,
                    text=history_text or text,
                    reply_to_message_id=reply_to,
                )
            )

        if self._busy and await self._accept_question_answer(message, text):
            return

        LOGGER.info(
            "Telegram message received message_id=%s reply_to=%s images=%d",
            message.message_id,
            reply_to,
            len(image_paths),
        )
        prompt = turn_text(text, message.reply_to_message)
        if self._busy:
            LOGGER.info(
                "Telegram message steering active turn message_id=%s",
                message.message_id,
            )
            await self._accept_followup(
                _PendingMessage(
                    message=message,
                    user_id=user_id,
                    text=text,
                    image_paths=image_paths,
                ),
                prompt,
            )
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
        if self._rich_api is None:
            raise RuntimeError("Telegram Rich Messages are not initialized.")
        live = LiveTurn(message, self._rich_api, self._history)
        self._live_response = live
        try:
            await live.start()
            if self._stopping:
                await self._send_stopped()
                return

            try:
                await self._stream_response(live, prompt, image_paths)
                status = "success"
            except TurnInterrupted:
                status = "cancelled"
                LOGGER.info(
                    "Telegram turn interrupted message_id=%s", message.message_id
                )
                await self._send_stopped()
            except Exception:
                if self._stopping:
                    status = "cancelled"
                    LOGGER.info(
                        "Telegram turn stopped while an operation was failing "
                        "message_id=%s",
                        message.message_id,
                    )
                    await self._send_stopped()
                else:
                    status = "failure"
                    LOGGER.exception(
                        "Telegram turn failed message_id=%s", message.message_id
                    )
                    await live.fail()
        finally:
            await self._cancel_pending_question()
            self._stop_notice = None
            self._stopping = False
            self._live_response = None
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
    ) -> None:
        """Send one file to Codex, or hold it for the rest of its media group.

        Telegram delivers an album as one update per file, so a media group is
        gathered behind a short sliding timer and sent as a single turn.
        """
        group = message.media_group_id
        if group is None:
            await self._submit_album(_Album(message, user_id, [attachment]))
            return

        album = self._albums.get(group)
        if album is None:
            album = _Album(message, user_id)
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
            text = build_document_turn_prompt(caption, documents)
        else:
            text = build_image_turn_prompt(caption, image_count=len(images))

        await self.handle_text(
            album.message,
            album.user_id,
            text,
            image_paths=images,
            history_text=_album_history_text(album, caption),
            content_type="document" if documents else "photo",
        )

    async def _accept_followup(
        self,
        pending: _PendingMessage,
        prompt: str,
    ) -> None:
        """Steer now when possible; otherwise retain the input without loss."""
        async with self._steer_lock:
            if self._pending_messages:
                self._pending_messages.append(pending)
                self._ensure_pending_task()
                return
            try:
                steered = await self._conversation.steer(
                    prompt, image_paths=pending.image_paths
                )
            except Exception:
                LOGGER.exception(
                    "Codex turn steering failed; preserving input for next turn"
                )
                pending.force_next_turn = True
                self._pending_messages.append(pending)
                self._ensure_pending_task()
                return

            if steered:
                LOGGER.info(
                    "Telegram steering accepted message_id=%s",
                    pending.message.message_id,
                )
                return

            LOGGER.info(
                "Telegram steering buffered message_id=%s turn_not_ready=true",
                pending.message.message_id,
            )
            self._pending_messages.append(pending)
            self._ensure_pending_task()

    def _ensure_pending_task(self) -> None:
        """Start the one ordered coordinator that drains buffered inputs."""
        if self._pending_task is None or self._pending_task.done():
            self._pending_task = asyncio.create_task(self._drain_pending_messages())

    async def _drain_pending_messages(self) -> None:
        """Deliver every buffered input exactly once and in arrival order."""
        while self._pending_messages:
            pending: _PendingMessage | None = None
            wait_for_turn = False
            async with self._steer_lock:
                if not self._pending_messages:
                    continue
                candidate = self._pending_messages[0]
                if self._busy:
                    if self._stopping or candidate.force_next_turn:
                        wait_for_turn = True
                    else:
                        prompt = turn_text(
                            candidate.text,
                            candidate.message.reply_to_message,
                        )
                        try:
                            steered = await self._conversation.steer(
                                prompt, image_paths=candidate.image_paths
                            )
                        except Exception:
                            LOGGER.exception(
                                "Codex steering retry failed; moving input to next turn"
                            )
                            candidate.force_next_turn = True
                            wait_for_turn = True
                        else:
                            if steered:
                                self._pending_messages.popleft()
                                LOGGER.info(
                                    "Buffered Telegram steering accepted message_id=%s",
                                    candidate.message.message_id,
                                )
                                continue
                            wait_for_turn = True
                else:
                    pending = self._pending_messages.popleft()

            if pending is not None:
                await self.handle_text(
                    pending.message,
                    pending.user_id,
                    pending.text,
                    pending.image_paths,
                    record_history=False,
                )
                continue
            if wait_for_turn:
                await asyncio.sleep(0.05)

        self._pending_task = None

    async def _stream_response(
        self,
        live: LiveTurn,
        prompt: str,
        image_paths: tuple[Path, ...] = (),
    ) -> None:
        async for event in self._conversation.stream_turn(
            prompt,
            image_paths=image_paths,
            stop_requested=lambda: self._stopping,
        ):
            if not self._stopping:
                await live.apply(event)

        if self._stopping:
            raise TurnInterrupted()
        await live.complete()

    async def _send_stopped(self) -> None:
        if self._live_response is not None:
            await self._live_response.stopped()
        notice = self._stop_notice
        if notice is not None and await self._edit_safely(notice, STOPPED_MESSAGE):
            return

    async def _accept_question_answer(self, message: Message, text: str) -> bool:
        """Consume ordinary text as the pending tool question's answer."""
        try:
            question = self._questions.answer_text(message.chat_id, text)
        except (OSError, sqlite3.Error):
            LOGGER.exception("Telegram typed-question state failed")
            return False
        if question is None:
            return False
        LOGGER.info(
            "Telegram question answered question_id=%s source=text message_id=%s",
            question.question_id,
            message.message_id,
        )
        await self._settle_question(question)
        return True

    async def _cancel_pending_question(self) -> None:
        """Expire the current turn's unanswered controls without losing state."""
        try:
            questions = self._questions.cancel_pending(self._allowed_user_id)
        except (OSError, sqlite3.Error):
            LOGGER.exception("Telegram question cancellation failed")
            return
        for question in questions:
            LOGGER.info(
                "Telegram question cancelled question_id=%s", question.question_id
            )
            await self._settle_question(question)

    async def _settle_question(self, question: TelegramQuestion) -> None:
        bot = self._bot
        if bot is None:
            return
        try:
            await TelegramQuestionCard(bot).settle(question)
        except TelegramError:
            LOGGER.exception(
                "Telegram question card update failed question_id=%s",
                question.question_id,
            )

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

    async def _answer_callback_safely(
        self, query: CallbackQuery, text: str | None = None
    ) -> None:
        try:
            await query.answer(text=text)
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
