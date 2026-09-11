"""Telegram adapter for Ariadne's conversation loop."""

import asyncio
import logging
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from telegram import (
    Bot,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    ReactionType,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReactionTypePaid,
    Update,
    Voice,
)
from telegram.constants import FileSizeLimit, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..codex import (
    CodexConversation,
    CodexModel,
    CodexTurnSettings,
    TurnInterrupted,
    WebSearchSetting,
)
from ..handoff import ConversationHandoff, HandoffCoordinator, HandoffState
from ..prompts.activations import (
    EMPTY_TELEGRAM_REPLY,
    build_direct_turn_with_handoffs,
    build_document_turn_prompt,
    build_image_turn_prompt,
    build_proactive_handoff_turn_prompt,
    build_telegram_turn_prompt,
)
from ..revisit.models import Revisit
from ..stewardship.models import StewardshipRuntimeSnapshot
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
    LiveTurn,
)
from .panels import TelegramControlPanel, TelegramControlPanelStore
from .proactive import ProactiveTurn
from .questions import (
    QuestionSelection,
    TelegramQuestion,
    TelegramQuestionCard,
    TelegramQuestionStore,
    default_question_state_path,
    parse_question_callback,
)
from .rich import RichBotAPI, incoming_rich_markdown
from .status import (
    STATUS_CALLBACK_PREFIX,
    STATUS_DELIVER_CALLBACK,
    STATUS_INITIATIVE_CALLBACK,
    STATUS_INITIATIVE_PAUSE_CALLBACK,
    STATUS_INITIATIVE_PAUSE_DAY_CALLBACK,
    STATUS_INITIATIVE_RESUME_CALLBACK,
    STATUS_INITIATIVE_RUN_CALLBACK,
    STATUS_ROOT_CALLBACK,
    STATUS_SETTINGS_CALLBACK,
    STATUS_WAKEUP_ASK_PREFIX,
    STATUS_WAKEUP_CONFIRM_PREFIX,
    STATUS_WAKEUPS_PREFIX,
    StatusSources,
    render_initiative,
    render_status,
    render_wakeup_cancellation,
    render_wakeups,
    wakeup_counts,
)
from .voice import VoiceTranscriber, VoiceTranscriptionError

LOGGER = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_BYTES = int(FileSizeLimit.FILESIZE_DOWNLOAD)
MAX_VOICE_BYTES = int(FileSizeLimit.FILESIZE_DOWNLOAD)
MAX_VOICE_DURATION_SECONDS = 10 * 60
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
VOICE_TOO_LARGE_MESSAGE = "That voice note is too large; the limit is 20 MB."
VOICE_TOO_LONG_MESSAGE = "That voice note is too long; the limit is 10 minutes."
VOICE_UNAVAILABLE_MESSAGE = "Voice-note transcription isn't configured yet."
VOICE_FAILED_MESSAGE = "I couldn't transcribe that voice note. Please try again."
NOTHING_TO_STOP_MESSAGE = "There isn't an active turn to stop."
WAKEUPS_UNAVAILABLE_MESSAGE = "Wake-ups\n\nI couldn't read the wake-up list."
SETTINGS_UNAVAILABLE_MESSAGE = (
    "I couldn't load the available Codex settings. Please try again."
)
SETTINGS_BUSY_MESSAGE = "Settings can't change while Ariadne is working."
CONTINUITY_LOST_MESSAGE = (
    "I couldn't recover our previous conversation after the restart, so I've "
    "started a fresh one. Your shared memory is still available."
)
RESET_FAILED_MESSAGE = "I couldn't safely start a fresh conversation. Please retry."

SETTINGS_CALLBACK_PREFIX = "settings:"
SETTINGS_MODELS_CALLBACK = "settings:models"
SETTINGS_EFFORT_CALLBACK = "settings:effort"
SETTINGS_WEB_CALLBACK = "settings:web"
SETTINGS_BACK_CALLBACK = "settings:back"
SETTINGS_STATUS_CALLBACK = "settings:status"


def turn_text(
    text: str,
    replied_message: object | None = None,
) -> str:
    """Extract Telegram reply content for the shared activation builder."""
    quoted_message: str | None = None
    if replied_message is not None:
        quoted_message = getattr(replied_message, "text", None)
        if quoted_message is None:
            quoted_message = getattr(replied_message, "caption", None)
        if quoted_message is None and isinstance(replied_message, Message):
            quoted_message = incoming_rich_markdown(replied_message)
        if quoted_message is None:
            quoted_message = EMPTY_TELEGRAM_REPLY
    author: str | None = None
    message_id: int | None = None
    if replied_message is not None:
        candidate_message_id = getattr(replied_message, "message_id", None)
        if isinstance(candidate_message_id, int):
            message_id = candidate_message_id
        sender = getattr(replied_message, "from_user", None)
        if sender is not None:
            author = "Iris" if bool(getattr(sender, "is_bot", False)) else "Divy"
    return build_telegram_turn_prompt(
        text,
        quoted_message=quoted_message,
        quoted_message_id=message_id,
        quoted_author=author,
    )


def _page_number(value: str) -> int:
    """Read a page from untrusted callback data without raising."""
    return int(value) if value.isdigit() and value != "0" else 1


def _status_back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("Back", callback_data=STATUS_ROOT_CALLBACK)


def _reaction_value(reaction: ReactionType) -> str:
    if isinstance(reaction, ReactionTypeEmoji):
        return reaction.emoji
    if isinstance(reaction, ReactionTypeCustomEmoji):
        return f"custom:{reaction.custom_emoji_id}"
    if isinstance(reaction, ReactionTypePaid):
        return "paid"
    return f"unknown:{reaction.type}"


def _document_filename(document: Document) -> str:
    """Return the sender's filename, stripped of any directory component."""
    name = Path(document.file_name or "").name
    return name if name not in {"", ".", ".."} else "document"


def _attachment_name(media: PhotoSize | Document | Voice) -> str:
    """Return the filename to keep a downloaded attachment under."""
    if isinstance(media, Document):
        return _document_filename(media)
    if isinstance(media, Voice):
        return f"voice-{datetime.now():%H%M%S}.ogg"
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
        handoff_coordinator: HandoffCoordinator | None = None,
        voice_transcriber: VoiceTranscriber | None = None,
        status_sources: StatusSources | None = None,
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
        self._handoffs = handoff_coordinator or HandoffCoordinator(
            HandoffState(state_path)
        )
        self._panels = TelegramControlPanelStore(state_path)
        self._panels.initialize()
        self._file_delivery = FileDelivery()
        self._albums: dict[str, _Album] = {}
        self._pending_messages: deque[_PendingMessage] = deque()
        self._pending_task: asyncio.Task[None] | None = None
        self._steer_lock = asyncio.Lock()
        self._voice_transcriber = voice_transcriber
        self._status = status_sources
        self._initiative_task: asyncio.Task[None] | None = None

    @property
    def proactive_handoff_blocked(self) -> bool:
        """Keep proactive work behind active or already-buffered human input."""
        return self._busy or self._stopping or bool(self._pending_messages)

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

    async def recover_panel(self) -> None:
        """Remove a deterministic panel left visible by a previous process."""
        await self._dismiss_panel(self._allowed_user_id)

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
        if not self._is_allowed(self._user_id_from(update)):
            return
        await self._answer_callback_safely(query)
        message = query.message
        if not isinstance(message, Message) or not isinstance(query.data, str):
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

        if not self._is_allowed(self._user_id_from(update)):
            await self._answer_callback_safely(query)
            return

        message = query.message
        if not isinstance(message, Message) or not isinstance(query.data, str):
            await self._answer_callback_safely(query)
            return
        if not self._panel_is_active(message, "settings"):
            await self._answer_callback_safely(
                query, "This settings panel is no longer active."
            )
            return
        await self._answer_callback_safely(query)
        await self.handle_settings_callback(
            message,
            self._user_id_from(update),
            query.data,
        )

    async def status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status."""
        message = self._message_from(update)
        if message is None:
            return
        await self.handle_status(message, self._user_id_from(update))

    async def wakeups(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /wakeups."""
        message = self._message_from(update)
        if message is None:
            return
        await self.handle_wakeups(message, self._user_id_from(update))

    async def status_callback(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle a button press from Ariadne's status panel."""
        query = update.callback_query
        if not isinstance(query, CallbackQuery):
            return

        if not self._is_allowed(self._user_id_from(update)):
            await self._answer_callback_safely(query)
            return

        message = query.message
        if not isinstance(message, Message) or not isinstance(query.data, str):
            await self._answer_callback_safely(query)
            return
        if not self._panel_is_active(message, "status"):
            await self._answer_callback_safely(query, "This panel is no longer active.")
            return
        await self._answer_callback_safely(query)
        await self.handle_status_callback(
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

    async def reaction(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        """Record an owner's current reaction without starting an agent turn."""
        changed = update.message_reaction
        if (
            changed is None
            or not self._is_allowed(self._user_id_from(update))
            or changed.chat.id != self._allowed_user_id
            or changed.chat.type != "private"
        ):
            return
        try:
            recorded = self._history.set_reactions(
                changed.chat.id,
                changed.message_id,
                tuple(_reaction_value(reaction) for reaction in changed.new_reaction),
                reacted_at=changed.date,
            )
        except (OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Telegram reaction state update failed")
            return
        if recorded:
            LOGGER.info(
                "Telegram reaction updated message_id=%s reactions=%d",
                changed.message_id,
                len(changed.new_reaction),
            )

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Download an image message and send it to Codex with its caption."""
        message = self._message_from(update)
        if message is None:
            return
        if not self._is_allowed(self._user_id_from(update)):
            return
        await self._dismiss_panel(message.chat_id)
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
        await self._dismiss_panel(message.chat_id)
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

    async def voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Transcribe an incoming voice note and continue the shared conversation."""
        message = self._message_from(update)
        if message is None or message.voice is None:
            return
        await self.handle_voice(
            message,
            self._user_id_from(update),
            message.voice,
            context,
        )

    async def handle_voice(
        self,
        message: Message,
        user_id: int | None,
        voice: Voice,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Validate, transcribe, and submit one Telegram voice note."""
        if not self._is_allowed(user_id):
            return
        await self._dismiss_panel(message.chat_id)
        if self._voice_transcriber is None:
            await self._reply_safely(message, VOICE_UNAVAILABLE_MESSAGE)
            return
        duration = (
            voice.duration.total_seconds()
            if isinstance(voice.duration, timedelta)
            else voice.duration
        )
        if duration > MAX_VOICE_DURATION_SECONDS:
            await self._reply_safely(message, VOICE_TOO_LONG_MESSAGE)
            return
        if voice.file_size is not None and voice.file_size > MAX_VOICE_BYTES:
            await self._reply_safely(message, VOICE_TOO_LARGE_MESSAGE)
            return
        try:
            path = await self._download(context, voice, MAX_VOICE_BYTES)
        except (OSError, TelegramError):
            LOGGER.exception("Voice note download failed")
            await self._reply_safely(message, VOICE_FAILED_MESSAGE)
            return
        try:
            transcript = await self._voice_transcriber.transcribe(path)
        except VoiceTranscriptionError:
            LOGGER.exception("Voice note transcription failed")
            await self._reply_safely(message, VOICE_FAILED_MESSAGE)
            return
        finally:
            path.unlink(missing_ok=True)

        prompt = (
            "Telegram voice-note transcript (automatic and possibly imperfect):\n"
            "<transcript>\n"
            f"{transcript}\n"
            "</transcript>"
        )
        await self.handle_text(
            message,
            user_id,
            prompt,
            history_text=f"[Voice note]\n{transcript}",
            content_type="voice",
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
        await self._send_panel(message, READY_MESSAGE, kind="start")

    async def handle_new(self, message: Message, user_id: int | None) -> None:
        """Start a fresh Codex session without changing private knowledge."""
        if not self._is_allowed(user_id):
            return
        if self._busy:
            await self._send_panel(message, BUSY_MESSAGE, kind="new")
            return

        try:
            self._conversation.reset()
        except Exception:
            LOGGER.exception("Persistent Codex conversation reset failed")
            await self._send_panel(message, RESET_FAILED_MESSAGE, kind="new")
            return
        await self._send_panel(message, NEW_CONVERSATION_MESSAGE, kind="new")

    async def handle_stop(self, message: Message, user_id: int | None) -> None:
        """Request that Codex stop the one active Ariadne turn."""
        if not self._is_allowed(user_id):
            return
        await self._request_stop(message)

    async def _request_stop(self, command_message: Message | None) -> None:
        """Interrupt the active turn in response to /stop."""
        if not self._busy:
            if command_message is not None:
                await self._send_panel(
                    command_message, NOTHING_TO_STOP_MESSAGE, kind="stop"
                )
            return
        if self._stopping:
            if command_message is not None:
                await self._delete_safely(command_message)
            return

        self._stopping = True
        await self._cancel_pending_question()
        if self._live_response is not None:
            if command_message is not None:
                await self._delete_safely(command_message)
            await self._live_response.stopping()
        elif command_message is not None:
            self._stop_notice = await self._send_panel(
                command_message, STOPPING_MESSAGE, kind="stop"
            )

        try:
            if not await self._conversation.interrupt():
                LOGGER.info("Stop requested before the Codex turn started")
        except Exception:
            LOGGER.exception("Codex turn interruption failed")
            if self._live_response is not None:
                await self._live_response.resume()
            self._stopping = False
            notice = self._stop_notice
            self._stop_notice = None
            if notice is not None:
                await self._edit_safely(
                    notice, "I couldn't stop the active turn. Please try again."
                )

    async def handle_settings(self, message: Message, user_id: int | None) -> None:
        """Show the process-local Codex settings panel."""
        if not self._is_allowed(user_id):
            return
        await self._send_panel(
            message,
            self._settings_text(),
            kind="settings",
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
        elif data == SETTINGS_STATUS_CALLBACK:
            if self._status is None:
                return
            self._rekey_panel(message, "status")
            await self._show_status(message)
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
        if not await self._apply_settings(
            message, replace(settings, model=model.identifier, effort=effort)
        ):
            return
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

        if not await self._apply_settings(
            message, replace(self._conversation.settings, effort=effort)
        ):
            return
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

        if not await self._apply_settings(
            message, replace(self._conversation.settings, web_search=web_search)
        ):
            return
        await self._show_settings(message)

    async def _apply_settings(
        self, message: Message, settings: CodexTurnSettings
    ) -> bool:
        """Change settings only after the durable old thread is forgotten."""
        try:
            self._conversation.set_settings(settings)
        except Exception:
            LOGGER.exception("Persistent Codex conversation reset failed")
            await self._edit_safely(message, RESET_FAILED_MESSAGE)
            return False
        return True

    async def _settings_can_change(self, message: Message) -> bool:
        if not self._busy:
            return True
        await self._edit_safely(message, SETTINGS_BUSY_MESSAGE)
        return False

    async def handle_status(self, message: Message, user_id: int | None) -> None:
        """Open the compact deterministic status panel."""
        if not self._is_allowed(user_id) or self._status is None:
            return
        text, keyboard = self._status_panel()
        await self._send_panel(message, text, kind="status", reply_markup=keyboard)

    async def handle_wakeups(self, message: Message, user_id: int | None) -> None:
        """Open the wake-ups page directly, skipping the status entry point."""
        if not self._is_allowed(user_id) or self._status is None:
            return
        text, keyboard = self._wakeups_panel(1)
        await self._send_panel(message, text, kind="status", reply_markup=keyboard)

    async def handle_status_callback(
        self,
        message: Message,
        user_id: int | None,
        data: str,
    ) -> None:
        """Apply one trusted status-panel control without starting a model turn."""
        if (
            not self._is_allowed(user_id)
            or self._status is None
            or not data.startswith(STATUS_CALLBACK_PREFIX)
        ):
            return

        if data == STATUS_ROOT_CALLBACK:
            await self._show_status(message)
        elif data == STATUS_INITIATIVE_CALLBACK:
            await self._show_initiative(message)
        elif data == STATUS_INITIATIVE_RUN_CALLBACK:
            await self._run_initiative_now(message)
        elif data == STATUS_INITIATIVE_PAUSE_DAY_CALLBACK:
            await self._pause_initiative(message, timedelta(hours=24))
        elif data == STATUS_INITIATIVE_PAUSE_CALLBACK:
            await self._pause_initiative(message, None)
        elif data == STATUS_INITIATIVE_RESUME_CALLBACK:
            await self._resume_initiative(message)
        elif data == STATUS_DELIVER_CALLBACK:
            await self._deliver_waiting_handoffs(message)
        elif data == STATUS_SETTINGS_CALLBACK:
            self._rekey_panel(message, "settings")
            await self._show_settings(message)
        elif data.startswith(STATUS_WAKEUPS_PREFIX):
            await self._show_wakeups(
                message, _page_number(data.removeprefix(STATUS_WAKEUPS_PREFIX))
            )
        elif data.startswith(STATUS_WAKEUP_ASK_PREFIX):
            await self._ask_wakeup_cancellation(
                message, data.removeprefix(STATUS_WAKEUP_ASK_PREFIX)
            )
        elif data.startswith(STATUS_WAKEUP_CONFIRM_PREFIX):
            await self._cancel_wakeup(
                message, data.removeprefix(STATUS_WAKEUP_CONFIRM_PREFIX)
            )

    async def _show_status(self, message: Message) -> None:
        text, keyboard = self._status_panel()
        await self._edit_safely(message, text, reply_markup=keyboard)

    async def _show_wakeups(self, message: Message, page: int) -> None:
        text, keyboard = self._wakeups_panel(page)
        await self._edit_safely(message, text, reply_markup=keyboard)

    async def _show_initiative(self, message: Message) -> None:
        text, keyboard = self._initiative_panel()
        await self._edit_safely(message, text, reply_markup=keyboard)

    def _status_panel(self) -> tuple[str, InlineKeyboardMarkup]:
        assert self._status is not None
        snapshot = self._initiative_snapshot()
        revisits = self._open_wakeups()
        waiting = self._waiting_handoffs()
        settings = self._conversation.settings
        text = render_status(
            working=self._busy,
            snapshot=snapshot,
            counts=wakeup_counts(revisits) if revisits is not None else None,
            waiting_handoffs=waiting,
            sources=self._status.sources,
            model=settings.model,
            effort=settings.effort.value,
            web_search="live" if settings.web_search == "live" else "off",
            timezone=self._status.timezone,
        )
        keyboard: list[list[InlineKeyboardButton]] = []
        if revisits is not None:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "Wake-ups", callback_data=f"{STATUS_WAKEUPS_PREFIX}1"
                    )
                ]
            )
        if snapshot is not None:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "Initiative", callback_data=STATUS_INITIATIVE_CALLBACK
                    )
                ]
            )
        if waiting:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"Deliver {waiting} waiting now",
                        callback_data=STATUS_DELIVER_CALLBACK,
                    )
                ]
            )
        keyboard.append(
            [InlineKeyboardButton("Settings", callback_data=STATUS_SETTINGS_CALLBACK)]
        )
        return text, InlineKeyboardMarkup(keyboard)

    def _wakeups_panel(self, page: int) -> tuple[str, InlineKeyboardMarkup]:
        assert self._status is not None
        revisits = self._open_wakeups()
        if revisits is None:
            return (
                WAKEUPS_UNAVAILABLE_MESSAGE,
                InlineKeyboardMarkup([[_status_back_button()]]),
            )
        text, shown, current, pages = render_wakeups(
            revisits, page=page, timezone=self._status.timezone
        )
        keyboard: list[list[InlineKeyboardButton]] = []
        cancels = [
            InlineKeyboardButton(
                f"Cancel {offset}",
                callback_data=f"{STATUS_WAKEUP_ASK_PREFIX}{revisit.id}",
            )
            for offset, revisit in enumerate(shown, start=1)
        ]
        keyboard += [cancels[index : index + 3] for index in range(0, len(cancels), 3)]
        if pages > 1:
            navigation: list[InlineKeyboardButton] = []
            if current > 1:
                navigation.append(
                    InlineKeyboardButton(
                        "Previous",
                        callback_data=f"{STATUS_WAKEUPS_PREFIX}{current - 1}",
                    )
                )
            if current < pages:
                navigation.append(
                    InlineKeyboardButton(
                        "Next", callback_data=f"{STATUS_WAKEUPS_PREFIX}{current + 1}"
                    )
                )
            keyboard.append(navigation)
        keyboard.append([_status_back_button()])
        return text, InlineKeyboardMarkup(keyboard)

    def _initiative_panel(self) -> tuple[str, InlineKeyboardMarkup]:
        assert self._status is not None
        snapshot = self._initiative_snapshot()
        text = render_initiative(snapshot, self._status.timezone)
        keyboard: list[list[InlineKeyboardButton]] = []
        if snapshot is not None and snapshot.enabled:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        "Run now", callback_data=STATUS_INITIATIVE_RUN_CALLBACK
                    )
                ]
            )
            if snapshot.paused:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "Resume", callback_data=STATUS_INITIATIVE_RESUME_CALLBACK
                        )
                    ]
                )
            else:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "Pause 24 hours",
                            callback_data=STATUS_INITIATIVE_PAUSE_DAY_CALLBACK,
                        ),
                        InlineKeyboardButton(
                            "Pause indefinitely",
                            callback_data=STATUS_INITIATIVE_PAUSE_CALLBACK,
                        ),
                    ]
                )
        keyboard.append([_status_back_button()])
        return text, InlineKeyboardMarkup(keyboard)

    async def _ask_wakeup_cancellation(self, message: Message, identifier: str) -> None:
        assert self._status is not None
        revisit = next(
            (item for item in self._open_wakeups() or () if item.id == identifier),
            None,
        )
        if revisit is None:
            await self._show_wakeups(message, 1)
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Cancel it",
                        callback_data=f"{STATUS_WAKEUP_CONFIRM_PREFIX}{revisit.id}",
                    ),
                    InlineKeyboardButton(
                        "Keep it", callback_data=f"{STATUS_WAKEUPS_PREFIX}1"
                    ),
                ]
            ]
        )
        await self._edit_safely(
            message,
            render_wakeup_cancellation(revisit, self._status.timezone),
            reply_markup=keyboard,
        )

    async def _cancel_wakeup(self, message: Message, identifier: str) -> None:
        assert self._status is not None
        directory = self._status.wakeups
        if directory is None:
            await self._show_status(message)
            return
        try:
            directory.cancel(identifier)
        except (OSError, sqlite3.Error, ValueError):
            # A wake-up that already ran or vanished is not an error worth
            # escalating; show the owner the list as it now really is.
            LOGGER.info("Telegram wake-up cancellation was rejected", exc_info=True)
        await self._show_wakeups(message, 1)

    async def _run_initiative_now(self, message: Message) -> None:
        assert self._status is not None
        initiative = self._status.initiative
        if initiative is None:
            await self._show_initiative(message)
            return
        if self._initiative_task is not None and not self._initiative_task.done():
            await self._show_initiative(message)
            return

        async def run() -> None:
            try:
                result = await initiative.process_due(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Requested stewardship cycle failed")
            else:
                LOGGER.info("Requested stewardship cycle finished %s", result.status)

        # A cycle is a long background turn; the panel must stay responsive.
        self._initiative_task = asyncio.create_task(run())
        await self._show_initiative(message)

    async def _pause_initiative(
        self, message: Message, duration: timedelta | None
    ) -> None:
        assert self._status is not None
        initiative = self._status.initiative
        if initiative is not None:
            until = datetime.now(UTC) + duration if duration is not None else None
            try:
                initiative.pause(until=until)
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception("Telegram initiative pause failed")
        await self._show_initiative(message)

    async def _resume_initiative(self, message: Message) -> None:
        assert self._status is not None
        initiative = self._status.initiative
        if initiative is not None:
            try:
                initiative.resume()
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception("Telegram initiative resume failed")
        await self._show_initiative(message)

    async def _deliver_waiting_handoffs(self, message: Message) -> None:
        """Ask for waiting context now without exposing an inbox interface."""
        try:
            delivered = await self._handoffs.deliver_now(self)
        except Exception:
            LOGGER.exception("Requested handoff delivery failed")
            delivered = False
        if delivered:
            # Delivery is a conversational turn, so it has already removed this
            # panel. Editing the deleted message would only log a failure.
            return
        await self._show_status(message)

    def _initiative_snapshot(self) -> StewardshipRuntimeSnapshot | None:
        assert self._status is not None
        if self._status.initiative is None:
            return None
        try:
            return self._status.initiative.status_snapshot()
        except (OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Telegram initiative status read failed")
            return None

    def _open_wakeups(self) -> tuple[Revisit, ...] | None:
        assert self._status is not None
        if self._status.wakeups is None:
            return None
        try:
            return self._status.wakeups.list_open()
        except (OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Telegram wake-up list read failed")
            return None

    def _waiting_handoffs(self) -> int:
        try:
            return self._handoffs.waiting_count()
        except (OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Telegram waiting-handoff count failed")
            return 0

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
        self._handoffs.note_activity()
        await self._dismiss_panel(message.chat_id)

        reply_to = (
            message.reply_to_message.message_id
            if message.reply_to_message is not None
            else None
        )
        if (
            self._live_response is not None
            and reply_to == self._live_response.message_id
        ):
            reply_to = None
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

        if self._rich_api is None:
            raise RuntimeError("Telegram Rich Messages are not initialized.")
        claimed_handoffs = self._handoffs.claim_for_direct_turn()
        prompt = build_direct_turn_with_handoffs(prompt, claimed_handoffs)
        started_at = time.monotonic()
        status = "cancelled"
        self._busy = True
        LOGGER.info(
            "Telegram turn started message_id=%s model=%s effort=%s",
            message.message_id,
            self._conversation.settings.model,
            self._conversation.settings.effort.value,
        )
        live = LiveTurn(message, self._rich_api, self._history)
        self._live_response = live
        try:
            await live.start()
            try:
                if await self._conversation.prepare_thread():
                    await self._reply_safely(message, CONTINUITY_LOST_MESSAGE)
                if self._stopping:
                    await self._send_stopped()
                    return
                await self._stream_response(live, prompt, image_paths)
                status = "success"
            except asyncio.CancelledError:
                status = "cancelled"
                await live.discard()
                raise
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
            if claimed_handoffs:
                if status == "success":
                    self._handoffs.complete(claimed_handoffs)
                else:
                    self._handoffs.retry(
                        claimed_handoffs,
                        f"Direct shared turn ended with status {status}",
                    )
            await self._cancel_pending_question()
            self._stop_notice = None
            self._stopping = False
            self._live_response = None
            self._busy = False
            self._handoffs.note_activity()
            LOGGER.info(
                "Telegram turn finished message_id=%s status=%s duration=%.2fs",
                message.message_id,
                status,
                time.monotonic() - started_at,
            )

    async def present_handoffs(self, handoffs: tuple[ConversationHandoff, ...]) -> None:
        """Run a quiet-window turn on the same conversation without fake input."""
        if self.proactive_handoff_blocked:
            raise RuntimeError("The Telegram conversation became busy.")
        rich_api = self._rich_api
        if rich_api is None:
            raise RuntimeError("Telegram Rich Messages are not initialized.")
        # A proactive turn is a conversational entry path too; a control panel
        # must not stay visible above speech the owner did not ask for.
        await self._dismiss_panel(self._allowed_user_id)
        self._busy = True
        started_at = time.monotonic()
        renderer = ProactiveTurn(
            rich_api,
            self._history,
            chat_id=self._allowed_user_id,
            activity=self._handoffs.note_activity,
        )
        try:
            prompt = build_proactive_handoff_turn_prompt(handoffs)
            async for event in self._conversation.stream_turn(
                prompt,
                stop_requested=lambda: self._stopping,
            ):
                if not self._stopping:
                    await renderer.apply(event)
            if self._stopping:
                raise TurnInterrupted()
            renderer.complete()
        except TurnInterrupted:
            await self._send_stopped()
            raise
        except Exception:
            if self._stopping:
                await self._send_stopped()
            raise
        finally:
            await self._cancel_pending_question()
            self._stop_notice = None
            self._stopping = False
            self._busy = False
            LOGGER.info(
                "Proactive shared turn finished handoffs=%d messages=%d duration=%.2fs",
                len(handoffs),
                renderer.delivered_messages,
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
        # Only the wait is cancellable: a later item restarts the timer. Once
        # submission begins, cancellation must propagate rather than be
        # swallowed into an apparently successful task.
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
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
        disable_notification: bool = False,
    ) -> Message | None:
        try:
            return await message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
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

    async def _send_panel(
        self,
        source: Message,
        text: str,
        *,
        kind: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        await self._dismiss_panel(source.chat_id)
        panel = await self._reply_safely(
            source,
            text,
            reply_markup=reply_markup,
            disable_notification=True,
        )
        if panel is not None:
            try:
                self._panels.set(
                    TelegramControlPanel(source.chat_id, panel.message_id, kind)
                )
            except (OSError, sqlite3.Error, ValueError):
                LOGGER.exception("Telegram control panel state update failed")
        await self._delete_safely(source)
        return panel

    def _rekey_panel(self, message: Message, kind: str) -> None:
        """Hand the one live panel to another control tree without resending it."""
        try:
            self._panels.set(
                TelegramControlPanel(message.chat_id, message.message_id, kind)
            )
        except (OSError, sqlite3.Error, ValueError):
            LOGGER.exception("Telegram control panel state update failed")

    def _panel_is_active(self, message: Message, kind: str) -> bool:
        try:
            panel = self._panels.get(message.chat_id)
        except (OSError, sqlite3.Error):
            LOGGER.exception("Telegram control panel state read failed")
            return False
        return (
            panel is not None
            and panel.message_id == message.message_id
            and panel.kind == kind
        )

    async def _dismiss_panel(self, chat_id: int) -> None:
        try:
            panel = self._panels.get(chat_id)
        except (OSError, sqlite3.Error):
            LOGGER.exception("Telegram control panel state read failed")
            return
        if panel is None or self._bot is None:
            return
        try:
            await self._bot.delete_message(chat_id, panel.message_id)
        except BadRequest:
            LOGGER.info(
                "Telegram control panel was already unavailable message_id=%s",
                panel.message_id,
            )
        except TelegramError:
            LOGGER.exception(
                "Telegram control panel deletion failed message_id=%s",
                panel.message_id,
            )
        try:
            self._panels.clear(chat_id, expected_message_id=panel.message_id)
        except (OSError, sqlite3.Error):
            LOGGER.exception("Telegram control panel state cleanup failed")

    @staticmethod
    async def _delete_safely(message: Message) -> None:
        try:
            await message.delete()
        except TelegramError:
            LOGGER.info(
                "Telegram command cleanup failed message_id=%s", message.message_id
            )

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
            "Changes start a fresh Codex conversation."
        )

    def _settings_keyboard(self) -> InlineKeyboardMarkup:
        settings = self._conversation.settings
        web_search = "Live" if settings.web_search == "live" else "Off"
        keyboard = [
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
        if self._status is not None:
            # The status tree owns this same panel; hand it back rather than
            # leaving the owner in a dead end after arriving from /status.
            keyboard.append(
                [InlineKeyboardButton("Status", callback_data=SETTINGS_STATUS_CALLBACK)]
            )
        return InlineKeyboardMarkup(keyboard)

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
        media: PhotoSize | Document | Voice,
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
