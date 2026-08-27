"""Forward-compatible access to Telegram Bot API Rich Messages.

python-telegram-bot intentionally exposes ``Bot.do_api_request`` and
``TelegramObject.api_kwargs`` so applications can use new Bot API features
before the library has native classes for them.  Rich Messages arrived in Bot
API 10.1, while PTB 22.8 natively models Bot API 10.0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any, Literal, cast

from telegram import Bot, Message

RICH_MESSAGE_LIMIT = 32_768

ButtonKind = Literal["callback_data", "url", "copy_text", "disabled"]
ButtonStyle = Literal["danger", "success", "primary", "link"]


@dataclass(frozen=True, slots=True)
class RichButton:
    """One native button embedded in a Telegram Rich Message."""

    text: str
    kind: ButtonKind
    value: str | None = None
    style: ButtonStyle | None = None

    def html(self) -> str:
        """Render the Bot API's allow-listed ``tg-button`` representation."""
        attributes = [f'type="{self.kind}"']
        if self.style is not None:
            attributes.append(f'style="{self.style}"')
        if self.kind == "callback_data":
            if not self.value:
                raise ValueError("A callback button needs callback data.")
            attributes.append(f'data="{escape(self.value, quote=True)}"')
        elif self.kind == "url":
            if not self.value:
                raise ValueError("A URL button needs a URL.")
            attributes.append(f'url="{escape(self.value, quote=True)}"')
        elif self.kind == "copy_text":
            if self.value is None:
                raise ValueError("A copy button needs text to copy.")
            attributes.append(f'text="{escape(self.value, quote=True)}"')
        elif self.value is not None:
            raise ValueError("A disabled button can't have a value.")
        return f"<tg-button {' '.join(attributes)}>{escape(self.text)}</tg-button>"


def rich_markdown(
    markdown: str,
    *,
    buttons: Sequence[RichButton] = (),
    button_alignment: Literal["left", "center", "right"] = "right",
    buttons_per_row: int = 8,
) -> dict[str, Any]:
    """Build an ``InputRichMessage`` using Telegram's Rich Markdown syntax."""
    if not 1 <= buttons_per_row <= 8:
        raise ValueError("A Rich Message button row needs between 1 and 8 buttons.")
    content = close_unterminated_fence(markdown.strip() or "…")
    if buttons:
        rows = []
        for offset in range(0, len(buttons), buttons_per_row):
            rendered = "\n".join(
                button.html() for button in buttons[offset : offset + buttons_per_row]
            )
            rows.append(
                f'<tg-button-row align="{button_alignment}">\n'
                f"{rendered}\n"
                "</tg-button-row>"
            )
        content += "\n\n" + "\n\n".join(rows)
    return {"markdown": content}


def close_unterminated_fence(markdown: str) -> str:
    """Close a partial fenced-code block before appending trusted controls."""
    fence: str | None = None
    for line in markdown.splitlines():
        marker = line.lstrip()[:3]
        if marker not in {"```", "~~~"}:
            continue
        if fence is None:
            fence = marker
        elif marker == fence:
            fence = None
    if fence is None:
        return markdown
    separator = "" if markdown.endswith("\n") else "\n"
    return f"{markdown}{separator}{fence}"


class RichBotAPI:
    """Small typed facade over PTB's documented forward-compatibility method."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(
        self,
        *,
        chat_id: int,
        markdown: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
        buttons: Sequence[RichButton] = (),
        buttons_per_row: int = 8,
    ) -> Message:
        """Send one persistent Rich Markdown message."""
        return await self.send_payload(
            chat_id=chat_id,
            rich_message=rich_markdown(
                markdown, buttons=buttons, buttons_per_row=buttons_per_row
            ),
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
        )

    async def send_payload(
        self,
        *,
        chat_id: int,
        rich_message: Mapping[str, Any],
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> Message:
        """Send any valid ``InputRichMessage``, including explicit block trees."""
        arguments: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": dict(rich_message),
        }
        if reply_to_message_id is not None:
            arguments["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        if message_thread_id is not None:
            arguments["message_thread_id"] = message_thread_id
        return cast(
            Message,
            await self._bot.do_api_request(
                "sendRichMessage", api_kwargs=arguments, return_type=Message
            ),
        )

    async def edit(
        self,
        message: Message,
        markdown: str,
        *,
        buttons: Sequence[RichButton] = (),
        buttons_per_row: int = 8,
    ) -> Message:
        """Replace an ordinary or Rich Message with Rich Markdown."""
        return await self.edit_payload(
            message,
            rich_markdown(markdown, buttons=buttons, buttons_per_row=buttons_per_row),
        )

    async def edit_by_id(
        self,
        *,
        chat_id: int,
        message_id: int,
        markdown: str,
        buttons: Sequence[RichButton] = (),
        buttons_per_row: int = 8,
    ) -> Message:
        """Edit a Rich Message when only its durable Telegram identity is known."""
        return await self.edit_payload_by_id(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=rich_markdown(
                markdown, buttons=buttons, buttons_per_row=buttons_per_row
            ),
        )

    async def edit_payload(
        self, message: Message, rich_message: Mapping[str, Any]
    ) -> Message:
        """Edit a message with any valid ``InputRichMessage`` block tree."""
        return await self.edit_payload_by_id(
            chat_id=message.chat_id,
            message_id=message.message_id,
            rich_message=rich_message,
        )

    async def edit_payload_by_id(
        self,
        *,
        chat_id: int,
        message_id: int,
        rich_message: Mapping[str, Any],
    ) -> Message:
        """Edit a message by ID with any valid ``InputRichMessage`` block tree."""
        return cast(
            Message,
            await self._bot.do_api_request(
                "editMessageText",
                api_kwargs={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "rich_message": dict(rich_message),
                },
                return_type=Message,
            ),
        )


def split_rich_markdown(markdown: str, limit: int = RICH_MESSAGE_LIMIT) -> list[str]:
    """Split Markdown at complete block boundaries for Rich Message delivery.

    Rich Messages are large enough that this is normally a one-item list.  The
    splitter avoids the old behavior of dropping all formatting merely because
    an answer crosses Telegram's classic 4096-character boundary.
    """
    if limit < 1:
        raise ValueError("The Rich Message limit must be positive.")
    markdown = close_unterminated_fence(markdown)
    if len(markdown) <= limit:
        return [markdown] if markdown else []

    chunks: list[str] = []
    current = ""
    for block in _markdown_blocks(markdown):
        separator = "" if not current or current.endswith("\n") else "\n\n"
        candidate = current + separator + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current.rstrip())
            current = ""
        if len(block) <= limit:
            current = block
            continue
        long_parts = _split_long_block(block, limit)
        chunks.extend(part.rstrip() for part in long_parts[:-1])
        current = long_parts[-1]
    if current:
        chunks.append(current.rstrip())
    return chunks


def _markdown_blocks(markdown: str) -> list[str]:
    """Return blank-line-delimited blocks without splitting fenced code."""
    blocks: list[str] = []
    lines: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
        if not line.strip() and fence is None:
            if lines:
                blocks.append("".join(lines).rstrip())
                lines = []
            continue
        lines.append(line)
    if lines:
        blocks.append("".join(lines).rstrip())
    return blocks


def _split_long_block(block: str, limit: int) -> list[str]:
    """Split an exceptional oversized block, preserving fenced-code syntax."""
    lines = block.splitlines(keepends=True)
    if len(lines) >= 2:
        opening = lines[0]
        marker = opening.lstrip()[:3]
        closing = lines[-1]
        if marker in {"```", "~~~"} and closing.lstrip().startswith(marker):
            room = limit - len(opening) - len(closing) - 1
            if room > 0:
                body = "".join(lines[1:-1])
                return [
                    opening + part.rstrip("\n") + "\n" + closing
                    for part in _split_text(body, room)
                ]
    return _split_text(block, limit)


def _split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = -1
        for separator in ("\n", " "):
            candidate = remaining.rfind(separator, 0, limit + 1)
            if candidate >= limit // 2:
                split_at = candidate + len(separator)
                break
        if split_at < 1:
            split_at = limit
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        parts.append(remaining)
    return parts


def incoming_rich_markdown(message: Message) -> str | None:
    """Convert PTB's raw, not-yet-native ``rich_message`` field to Markdown."""
    rich = message.api_kwargs.get("rich_message")
    if not isinstance(rich, Mapping):
        return None
    blocks = rich.get("blocks")
    if not isinstance(blocks, list):
        return None
    rendered = [_render_block(block) for block in blocks if isinstance(block, Mapping)]
    text = "\n\n".join(part for part in rendered if part).strip()
    return text or None


def _render_rich_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_render_rich_text(part) for part in value)
    if not isinstance(value, Mapping):
        return ""

    kind = value.get("type")
    text = _render_rich_text(value.get("text"))
    wrappers = {
        "bold": ("**", "**"),
        "italic": ("*", "*"),
        "underline": ("<u>", "</u>"),
        "strikethrough": ("~~", "~~"),
        "spoiler": ("||", "||"),
        "subscript": ("<sub>", "</sub>"),
        "superscript": ("<sup>", "</sup>"),
        "marked": ("==", "=="),
        "code": ("`", "`"),
    }
    if kind in wrappers:
        before, after = wrappers[cast(str, kind)]
        return f"{before}{text}{after}"
    if kind == "mathematical_expression":
        expression = value.get("expression")
        return f"${expression}$" if isinstance(expression, str) else text
    if kind == "custom_emoji":
        alternative = value.get("alternative_text")
        return alternative if isinstance(alternative, str) else ""
    if kind in {"url", "email_address", "phone_number"}:
        target_fields = {
            "url": "url",
            "email_address": "email_address",
            "phone_number": "phone_number",
        }
        target = value.get(
            target_fields[cast(Literal["url", "email_address", "phone_number"], kind)]
        )
        return f"[{text}]({target})" if isinstance(target, str) else text
    if kind == "button":
        button = value.get("button")
        if isinstance(button, Mapping):
            return f"[{_render_rich_text(button.get('text'))}]"
    return text


def _render_block(block: Mapping[object, object]) -> str:
    kind = block.get("type")
    if kind == "paragraph":
        return _render_rich_text(block.get("text"))
    if kind == "heading":
        size = block.get("size")
        level = min(max(size if isinstance(size, int) else 2, 1), 6)
        return f"{'#' * level} {_render_rich_text(block.get('text'))}"
    if kind == "pre":
        language = block.get("language")
        info = language if isinstance(language, str) else ""
        return f"```{info}\n{_render_rich_text(block.get('text'))}\n```"
    if kind == "footer":
        return _render_rich_text(block.get("text"))
    if kind == "divider":
        return "---"
    if kind == "mathematical_expression":
        expression = block.get("expression")
        return f"$$\n{expression}\n$$" if isinstance(expression, str) else ""
    if kind == "list":
        items = block.get("items")
        if not isinstance(items, list):
            return ""
        rendered_items: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            nested = item.get("blocks")
            content = (
                "\n\n".join(
                    _render_block(child)
                    for child in nested
                    if isinstance(child, Mapping)
                )
                if isinstance(nested, list)
                else ""
            )
            label = item.get("label")
            if item.get("has_checkbox") is True:
                marker = "- [x]" if item.get("is_checked") is True else "- [ ]"
            else:
                marker = label if isinstance(label, str) and label else "-"
            rendered_items.append(f"{marker} {content}".rstrip())
        return "\n".join(rendered_items)
    if kind in {"blockquote", "expandable_blockquote"}:
        nested = block.get("blocks")
        if isinstance(nested, list):
            content = "\n\n".join(
                _render_block(child) for child in nested if isinstance(child, Mapping)
            )
        else:
            content = _render_rich_text(block.get("text"))
        quoted = "\n".join(f"> {line}" for line in content.splitlines())
        credit = _render_rich_text(block.get("credit"))
        return f"{quoted}\n> — {credit}" if credit else quoted
    if kind == "pullquote":
        quoted = f"> {_render_rich_text(block.get('text'))}"
        credit = _render_rich_text(block.get("credit"))
        return f"{quoted}\n> — {credit}" if credit else quoted
    if kind == "details":
        summary = _render_rich_text(block.get("summary"))
        nested = block.get("blocks")
        content = (
            "\n\n".join(
                _render_block(child) for child in nested if isinstance(child, Mapping)
            )
            if isinstance(nested, list)
            else ""
        )
        return f"<details><summary>{summary}</summary>\n{content}\n</details>"
    if kind == "table":
        cells = block.get("cells")
        if not isinstance(cells, list):
            return ""
        rows = [
            "| "
            + " | ".join(
                _render_rich_text(cell.get("text")).replace("|", "\\|")
                for cell in row
                if isinstance(cell, Mapping)
            )
            + " |"
            for row in cells
            if isinstance(row, list)
        ]
        if not rows:
            return ""
        first_row = cells[0] if isinstance(cells[0], list) else []
        column_count = max(sum(1 for cell in first_row if isinstance(cell, Mapping)), 1)
        divider = "| " + " | ".join("---" for _ in range(column_count)) + " |"
        table = "\n".join((rows[0], divider, *rows[1:]))
        caption = _render_rich_text(block.get("caption"))
        return f"{caption}\n\n{table}" if caption else table
    if kind == "buttons":
        buttons = block.get("buttons")
        if not isinstance(buttons, list):
            return ""
        return " ".join(
            f"[{_render_rich_text(button.get('text'))}]"
            for button in buttons
            if isinstance(button, Mapping)
        )
    media_names = {
        "animation": "Animation",
        "audio": "Audio",
        "document": "Document",
        "map": "Map",
        "photo": "Photo",
        "video": "Video",
        "voice_note": "Voice note",
        "collage": "Collage",
        "slideshow": "Slideshow",
    }
    if kind in media_names:
        details: list[str] = []
        media = block.get(kind)
        if isinstance(media, Mapping):
            for field in ("file_name", "title", "performer"):
                value = media.get(field)
                if isinstance(value, str) and value and value not in details:
                    details.append(value)
        if kind == "map":
            latitude, longitude = block.get("latitude"), block.get("longitude")
            if isinstance(latitude, int | float) and isinstance(longitude, int | float):
                details.append(f"{latitude}, {longitude}")
        label = media_names[kind]
        if details:
            label += ": " + " — ".join(details)
        media_caption = block.get("caption")
        caption_text = (
            _render_rich_text(media_caption.get("text"))
            if isinstance(media_caption, Mapping)
            else ""
        )
        if caption_text:
            label += f" — {caption_text}"
        return f"[{label}]"
    return _render_rich_text(block.get("text"))
