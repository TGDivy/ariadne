"""Telegram adapter for Ariadne's conversation loop."""

import asyncio
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
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
from telegram.constants import ChatAction, FileSizeLimit, ParseMode
from telegram.error import BadRequest, EndPointNotFound, TelegramError, TimedOut
from telegram.ext import ContextTypes

from ..codex import CodexConversation, CodexModel, TurnInterrupted, WebSearchSetting
from ..prompt import THREAD_PUSH_PERMISSION
from .file_delivery import FileDelivery, FileDeliveryError
from .format import render_telegram_html, split_for_telegram, telegram_messages
from .questions import (
    QuestionSelection,
    TelegramQuestion,
    TelegramQuestionCard,
    TelegramQuestionStore,
    default_question_state_path,
    parse_question_callback,
)
from .rich import (
    RICH_MESSAGE_LIMIT,
    RichBotAPI,
    RichButton,
    close_unterminated_fence,
    incoming_rich_markdown,
    split_rich_markdown,
)

LOGGER = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = int(FileSizeLimit.FILESIZE_DOWNLOAD)
ATTACHMENT_ROOT = Path.home() / ".ariadne" / "attachments"
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
LIVE_EDIT_INTERVAL_SECONDS = 1.0
TURN_REFRESH_INTERVAL_SECONDS = 4.0
ALBUM_DEBOUNCE_SECONDS = 1.0
READY_MESSAGE = "Ariadne is ready."
NEW_CONVERSATION_MESSAGE = "Started a new conversation. The Thread is still available."
BUSY_MESSAGE = "I'm still working on your previous message."
IMAGE_WITHOUT_CAPTION = "Please inspect the attached image."
IMAGES_WITHOUT_CAPTION = "Please inspect the attached images."
DOCUMENT_WITHOUT_CAPTION = "I've sent you a file."
DOCUMENTS_WITHOUT_CAPTION = "I've sent you some files."
DOCUMENT_TOO_LARGE_MESSAGE = (
    "That file is too large; Telegram only lets me download files up to 20 MB."
)
DOCUMENT_FAILED_MESSAGE = "I couldn't download that file. Please try again."
FAILURE_MESSAGE = "I ran into a problem while working on that. Please try again."
STOPPING_MESSAGE = "Stopping…"
STOPPED_MESSAGE = "Stopped."
NOTHING_TO_STOP_MESSAGE = "There isn't an active turn to stop."
THINKING_MESSAGE = "Thinking…"
SETTINGS_UNAVAILABLE_MESSAGE = (
    "I couldn't load the available Codex settings. Please try again."
)
SETTINGS_BUSY_MESSAGE = "Settings can't change while Ariadne is working."

SETTINGS_CALLBACK_PREFIX = "settings:"
SETTINGS_MODELS_CALLBACK = "settings:models"
SETTINGS_EFFORT_CALLBACK = "settings:effort"
SETTINGS_WEB_CALLBACK = "settings:web"
SETTINGS_BACK_CALLBACK = "settings:back"
TURN_STOP_CALLBACK = "turn:stop"

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
            content = incoming_rich_markdown(replied_message)
        if content is None:
            content = "[The replied-to message has no text or caption.]"
        parts.append(
            f"Telegram reply context (message id {replied_message.message_id}):\n"
            f"<quoted_message>\n{content}\n</quoted_message>"
        )
    parts.extend((text, f"Telegram message id: {message_id}", THREAD_PUSH_PERMISSION))
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


class _LiveResponse:
    """One persistent Telegram message edited as an answer is generated."""

    _stop_button = RichButton(
        "Stop", "callback_data", TURN_STOP_CALLBACK, style="danger"
    )
    _stopping_button = RichButton("Stopping…", "disabled", style="danger")
    _stopped_button = RichButton("Stopped", "disabled", style="danger")

    def __init__(self, source: Message, rich_api: RichBotAPI | None) -> None:
        self._source = source
        self._rich_api = rich_api
        self._message: Message | None = None
        self._markdown = ""
        self._sent_at = 0.0
        self._rich = rich_api is not None
        self._phase = "running"
        self._edit_lock = asyncio.Lock()

    @property
    def message_id(self) -> int | None:
        """Return the live bubble's Telegram identity once it exists."""
        return self._message.message_id if self._message is not None else None

    async def start(self) -> None:
        """Create an ordinary persistent message with an embedded Stop control."""
        stopping = self._phase == "stopping"
        markdown = STOPPING_MESSAGE if stopping else THINKING_MESSAGE
        buttons = (self._stopping_button,) if stopping else (self._stop_button,)
        if self._rich_api is not None:
            try:
                self._message = await self._rich_api.send(
                    chat_id=self._source.chat_id,
                    markdown=markdown,
                    reply_to_message_id=self._source.message_id,
                    message_thread_id=getattr(self._source, "message_thread_id", None),
                    buttons=buttons,
                )
                self._markdown = markdown
                self._sent_at = time.monotonic()
                return
            except (BadRequest, EndPointNotFound):
                LOGGER.exception(
                    "Telegram Rich Message creation failed; using classic text"
                )
        self._rich = False
        self._message = await self._source.reply_text(
            markdown,
            reply_markup=None if stopping else self._classic_stop_keyboard(),
        )
        self._markdown = markdown
        self._sent_at = time.monotonic()

    async def show(self, markdown: str, *, force: bool = False) -> None:
        """Edit the live bubble with newly accumulated, natively formatted text."""
        if self._phase != "running":
            return
        preview = split_rich_markdown(markdown, RICH_MESSAGE_LIMIT - 1_024)[0]
        if preview == self._markdown:
            return
        if not force and time.monotonic() - self._sent_at < LIVE_EDIT_INTERVAL_SECONDS:
            return
        await self._edit(preview, buttons=(self._stop_button,), only_while_running=True)

    async def show_activity(self, activity: str) -> None:
        """Show a concise activity until answer text becomes available."""
        await self.show(activity)

    async def stopping(self) -> None:
        """Disable the control immediately after interruption is requested."""
        if self._phase != "running":
            return
        self._phase = "stopping"
        await self._edit(
            self._markdown or STOPPING_MESSAGE,
            buttons=(self._stopping_button,),
        )

    async def resume(self) -> None:
        """Restore the Stop control when Codex rejects an interruption request."""
        if self._phase != "stopping":
            return
        self._phase = "running"
        await self._edit(
            self._markdown or THINKING_MESSAGE,
            buttons=(self._stop_button,),
            only_while_running=True,
        )

    async def stopped(self, partial_response: str) -> None:
        """Preserve useful partial output and mark the message as stopped."""
        self._phase = "terminal"
        content = partial_response.strip()
        if content:
            content = close_unterminated_fence(content)
            content += f"\n\n_{STOPPED_MESSAGE}_"
        else:
            content = STOPPED_MESSAGE
        await self._finalize(content, buttons=(self._stopped_button,))

    async def finish(self, markdown: str) -> None:
        """Finalize this bubble and send any exceptional overflow as rich blocks."""
        if self._phase == "stopping":
            raise TurnInterrupted()
        if self._phase == "terminal":
            return
        self._phase = "terminal"
        await self._finalize(markdown, buttons=())

    async def _finalize(self, markdown: str, *, buttons: Sequence[RichButton]) -> None:
        """Persist complete content, retaining rich overflow and terminal controls."""
        chunks = split_rich_markdown(markdown)
        if not chunks:
            raise ValueError("A final Telegram response cannot be empty.")
        if not self._rich or self._rich_api is None:
            await self._finish_classic(markdown)
            return
        try:
            await self._edit(chunks[0], buttons=buttons, force_rich=True)
            for chunk in chunks[1:]:
                await self._rich_api.send(
                    chat_id=self._source.chat_id,
                    markdown=chunk,
                    message_thread_id=getattr(self._source, "message_thread_id", None),
                )
        except TelegramError:
            LOGGER.exception(
                "Telegram Rich Message finalization failed; using classic text"
            )
            await self._finish_classic(markdown)

    async def fail(self) -> None:
        """Turn the existing live bubble into a durable failure notice."""
        self._phase = "terminal"
        await self._durable_edit(FAILURE_MESSAGE, buttons=())

    async def discard(self) -> None:
        """Remove a placeholder when the agent already sent the same answer."""
        self._phase = "terminal"
        if self._message is None:
            return
        async with self._edit_lock:
            try:
                await self._message.delete()
            except TelegramError:
                LOGGER.exception("Telegram live placeholder deletion failed")

    async def _edit(
        self,
        markdown: str,
        *,
        buttons: Sequence[RichButton],
        force_rich: bool = False,
        only_while_running: bool = False,
    ) -> None:
        async with self._edit_lock:
            if only_while_running and self._phase != "running":
                return
            await self._edit_now(markdown, buttons=buttons, force_rich=force_rich)

    async def _edit_now(
        self,
        markdown: str,
        *,
        buttons: Sequence[RichButton],
        force_rich: bool,
    ) -> None:
        if self._message is None:
            return
        if self._rich and self._rich_api is not None:
            try:
                self._message = await self._rich_api.edit(
                    self._message, markdown, buttons=buttons
                )
                self._markdown = markdown
                self._sent_at = time.monotonic()
                return
            except TelegramError:
                if force_rich:
                    raise
                self._sent_at = time.monotonic()
                LOGGER.warning(
                    "Telegram Rich Message preview update failed; retaining preview"
                )
                return

        rendered = render_telegram_html(markdown)
        if len(rendered) > 4_096:
            rendered = render_telegram_html(split_for_telegram(markdown)[0])
        try:
            await self._message.edit_text(
                rendered,
                parse_mode=ParseMode.HTML,
                reply_markup=(
                    self._classic_stop_keyboard()
                    if any(button.kind == "callback_data" for button in buttons)
                    else None
                ),
            )
        except TelegramError:
            self._sent_at = time.monotonic()
            LOGGER.exception("Telegram classic live message update failed")
            return
        self._markdown = markdown
        self._sent_at = time.monotonic()

    async def _finish_classic(self, markdown: str) -> None:
        async with self._edit_lock:
            await self._finish_classic_now(markdown)

    async def _finish_classic_now(self, markdown: str) -> None:
        if self._message is None:
            return
        messages, is_html = telegram_messages(markdown)
        parse_mode = ParseMode.HTML if is_html else None
        try:
            await self._message.edit_text(messages[0], parse_mode=parse_mode)
        except TelegramError:
            LOGGER.exception("Telegram classic final edit failed")
            await self._source.reply_text(messages[0], parse_mode=parse_mode)
        for chunk in messages[1:]:
            await self._source.reply_text(chunk)
        self._markdown = markdown

    async def _durable_edit(
        self, markdown: str, *, buttons: Sequence[RichButton]
    ) -> None:
        """Use classic editing if Telegram rejects a terminal rich state."""
        try:
            await self._edit(markdown, buttons=buttons, force_rich=True)
        except TelegramError:
            LOGGER.exception(
                "Telegram Rich Message terminal edit failed; using classic text"
            )
            self._rich = False
            await self._edit(markdown, buttons=buttons)

    @staticmethod
    def _classic_stop_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=TURN_STOP_CALLBACK)]]
        )


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


@dataclass(slots=True)
class _PendingMessage:
    """One accepted Telegram input waiting to steer or start a Codex turn."""

    message: Message
    user_id: int | None
    text: str
    image_paths: tuple[Path, ...]
    send_typing: TypingSender | None
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
        self._live_response: _LiveResponse | None = None
        self._partial_response = ""
        self._bot: Bot | None = None
        self._rich_api: RichBotAPI | None = None
        self._questions = TelegramQuestionStore(
            question_state or default_question_state_path()
        )
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

        async def send_typing() -> None:
            await context.bot.send_chat_action(
                chat_id=message.chat_id,
                action=ChatAction.TYPING,
                message_thread_id=message.message_thread_id,
            )

        await self.handle_text(
            message,
            self._user_id_from(update),
            content,
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
        send_typing: TypingSender | None = None,
    ) -> None:
        """Send one user message through Codex and stream its answer back."""
        if not self._is_allowed(user_id):
            return

        if self._busy and await self._accept_question_answer(message, text):
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
            await self._accept_followup(
                _PendingMessage(
                    message=message,
                    user_id=user_id,
                    text=text,
                    image_paths=image_paths,
                    send_typing=send_typing,
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
        live = _LiveResponse(message, self._rich_api)
        self._live_response = live
        self._partial_response = ""
        live_turn: asyncio.Task[None] | None = None
        try:
            await live.start()
            live_turn = asyncio.create_task(self._keep_turn_alive(send_typing))
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
            if live_turn is not None:
                live_turn.cancel()
                with suppress(asyncio.CancelledError):
                    await live_turn
            await self._cancel_pending_question()
            self._stop_notice = None
            self._stopping = False
            self._live_response = None
            self._partial_response = ""
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
                            candidate.message.message_id,
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
                    send_typing=pending.send_typing,
                )
                continue
            if wait_for_turn:
                await asyncio.sleep(0.05)

        self._pending_task = None

    async def _keep_turn_alive(self, send_typing: TypingSender | None) -> None:
        """Keep Telegram's typing indicator active while Codex is working."""
        while True:
            if send_typing is not None:
                try:
                    await send_typing()
                except TimedOut:
                    LOGGER.warning("Telegram typing indicator timed out; will retry.")
                except TelegramError:
                    LOGGER.exception("Telegram typing indicator failed")
            await asyncio.sleep(TURN_REFRESH_INTERVAL_SECONDS)

    async def _stream_response(
        self,
        live: _LiveResponse,
        prompt: str,
        image_paths: tuple[Path, ...] = (),
    ) -> None:
        final_response = ""
        spoken: list[str] = []

        async def show_activity(activity: str) -> None:
            if final_response or self._stopping:
                return
            await live.show_activity(activity)

        async for response in self._conversation.stream_reply(
            prompt,
            image_paths=image_paths,
            activity=show_activity,
            spoken=spoken.append,
            stop_requested=lambda: self._stopping,
        ):
            final_response = response
            self._partial_response = response
            if not self._stopping:
                await live.show(response)

        if self._stopping:
            raise TurnInterrupted()
        if not final_response:
            raise RuntimeError("Codex completed without an agent response.")

        if _already_said(final_response, spoken):
            LOGGER.info("Iris already sent her answer herself; not repeating it")
            await live.discard()
            return

        await live.finish(final_response)

    async def _send_stopped(self) -> None:
        if self._live_response is not None:
            await self._live_response.stopped(self._partial_response)
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
