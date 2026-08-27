"""Forward-compatible access to Telegram Bot API Rich Messages.

python-telegram-bot intentionally exposes ``Bot.do_api_request`` and
``TelegramObject.api_kwargs`` so applications can use new Bot API features
before the library has native classes for them.  Rich Messages arrived in Bot
API 10.1, while PTB 22.8 natively models Bot API 10.0.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any, Literal, cast

from telegram import Bot, Message

RICH_MESSAGE_LIMIT = 32_768

ButtonKind = Literal["callback_data", "url", "copy_text", "disabled"]
ButtonStyle = Literal["danger", "success", "primary", "link"]
PendingRichKind = Literal[
    "code", "details", "formatting", "map", "math", "media", "table"
]

_FENCE_OPEN = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})", re.MULTILINE)
_TABLE_DELIMITER = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*"
    r"(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$"
)
_BUTTON = re.compile(
    r"<tg-button\b(?P<attributes>[^>]*)>(?P<text>.*?)</tg-button\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ANCHOR = re.compile(r"<a\b[^>]*>(?P<text>.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_DETAILS = re.compile(
    r"<details\b[^>]*>\s*<summary\b[^>]*>(?P<summary>.*?)</summary\s*>"
    r"(?P<body>.*?)</details\s*>",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_SPOILER = re.compile(r"(?<!\\)\|\|(?P<text>.*?)(?<!\\)\|\|", re.DOTALL)
_HTML_SPOILER = re.compile(
    r"<(?:tg-spoiler|spoiler)\b[^>]*>(?P<text>.*?)"
    r"</(?:tg-spoiler|spoiler)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_CODE = re.compile(
    r"<(?P<code_tag>pre|code)\b[^>]*>.*?</(?P=code_tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_INTERACTIVE_INLINE_TAG = re.compile(
    r"</?(?:tg-(?:date|mention|time|user))\b[^>]*>", re.IGNORECASE
)
_MEDIA_BLOCK = re.compile(
    r"<tg-(?P<name>animation|audio|collage|document|photo|slideshow|video|"
    r"voice-note)\b[^>]*>.*?</tg-(?P=name)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_MEDIA_SELF_CLOSING = re.compile(
    r"<tg-(?:animation|audio|collage|document|photo|slideshow|video|voice-note)"
    r"\b[^>]*/>",
    re.IGNORECASE,
)
_MAP_BLOCK = re.compile(r"<tg-map\b[^>]*>.*?</tg-map\s*>", re.IGNORECASE | re.DOTALL)
_MAP_SELF_CLOSING = re.compile(r"<tg-map\b[^>]*/>", re.IGNORECASE)

_VOID_HTML_TAGS = {"br", "hr", "img", "input", "link", "meta", "source"}
_RICH_HTML_TAGS = {
    "a",
    "b",
    "blockquote",
    "code",
    "del",
    "details",
    "div",
    "em",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "i",
    "ins",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strike",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}


@dataclass(frozen=True, slots=True)
class RichStreamPreview:
    """Structurally safe part of a cumulative Rich Markdown stream."""

    markdown: str
    pending: PendingRichKind | None = None

    @property
    def activity(self) -> str:
        """Describe only the structure that is actually still being composed."""
        activities: dict[PendingRichKind, str] = {
            "code": "Writing code…",
            "details": "Organising details…",
            "formatting": "Writing…",
            "map": "Composing a map…",
            "math": "Writing maths…",
            "media": "Preparing media…",
            "table": "Building a table…",
        }
        return activities[self.pending] if self.pending is not None else "Writing…"


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
    disable_interactions: bool = False,
) -> dict[str, Any]:
    """Build an ``InputRichMessage`` using Telegram's Rich Markdown syntax."""
    if not 1 <= buttons_per_row <= 8:
        raise ValueError("A Rich Message button row needs between 1 and 8 buttons.")
    content = close_unterminated_fence(markdown.strip() or "…")
    content = _disable_untrusted_buttons(content)
    if disable_interactions:
        content = _disable_streaming_interactions(content)
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
    result: dict[str, Any] = {"markdown": content}
    if disable_interactions:
        result["skip_entity_detection"] = True
    return result


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


def streaming_rich_preview(markdown: str) -> RichStreamPreview:
    """Return a calm preview that never exposes an unfinished rich construct.

    Codex deltas are cumulative, but they can stop between any two characters.
    Telegram reparses the whole message after every edit, so sending a dangling
    fence, link, table delimiter, formula, or HTML block can either expose raw
    source or make an otherwise valid edit fail.  Keep that tail behind the
    activity footer until its structure is complete instead.
    """
    text = markdown
    if not text.strip():
        return RichStreamPreview("")

    candidates: list[tuple[int, PendingRichKind]] = []
    scan_text, fence_candidate = _mask_fenced_code(text)
    if fence_candidate is not None:
        candidates.append(fence_candidate)

    scan_text, math_candidate = _mask_block_math(scan_text)
    if math_candidate is not None:
        candidates.append(math_candidate)

    scan_text, html_candidate = _mask_html_tags(scan_text)
    if html_candidate is not None:
        candidates.append(html_candidate)

    table_candidate = _incomplete_table(text, scan_text)
    if table_candidate is not None:
        candidates.append(table_candidate)

    inline_candidate = _incomplete_inline(scan_text)
    if inline_candidate is not None:
        candidates.append(inline_candidate)

    if candidates:
        end, pending = min(candidates, key=lambda candidate: candidate[0])
        text = text[:end].rstrip()
    else:
        pending = None

    return RichStreamPreview(
        _disable_streaming_interactions(text).rstrip(), pending=pending
    )


def _mask_fenced_code(
    text: str,
) -> tuple[str, tuple[int, PendingRichKind] | None]:
    """Mask complete fences and identify an unfinished fenced block."""
    ranges, opening = _fenced_code_ranges(text)
    candidate: tuple[int, PendingRichKind] | None = (
        (opening, "code") if opening is not None else None
    )
    return _mask_ranges(text, ranges), candidate


def _fenced_code_ranges(text: str) -> tuple[list[tuple[int, int]], int | None]:
    """Locate complete fenced blocks and the start of an incomplete one."""
    ranges: list[tuple[int, int]] = []
    opening: tuple[str, int] | None = None
    lines = text.splitlines(keepends=True)
    offset = 0
    for line in lines:
        match = _FENCE_OPEN.match(line)
        if match is not None:
            marker = match.group("fence")
            if opening is None:
                opening = (marker, offset)
            elif marker[0] == opening[0][0] and len(marker) >= len(opening[0]):
                remainder = line[match.end() :].strip()
                if not remainder:
                    ranges.append((opening[1], offset + len(line)))
                    opening = None
        offset += len(line)
    return ranges, opening[1] if opening is not None else None


def _mask_block_math(
    text: str,
) -> tuple[str, tuple[int, PendingRichKind] | None]:
    """Mask paired ``$$`` expressions outside already masked code."""
    ranges: list[tuple[int, int]] = []
    opening: int | None = None
    index = 0
    while index < len(text) - 1:
        if text[index : index + 2] != "$$" or _is_escaped(text, index):
            index += 1
            continue
        if opening is None:
            opening = index
        else:
            ranges.append((opening, index + 2))
            opening = None
        index += 2
    masked = _mask_ranges(text, ranges)
    return masked, (opening, "math") if opening is not None else None


def _mask_html_tags(
    text: str,
) -> tuple[str, tuple[int, PendingRichKind] | None]:
    """Mask tag syntax and retain an unclosed rich HTML container."""
    tag_ranges: list[tuple[int, int]] = []
    stack: list[tuple[str, int]] = []
    incomplete: tuple[int, PendingRichKind] | None = None
    index = 0
    while index < len(text):
        if text[index] != "<":
            index += 1
            continue
        if text.startswith("<!--", index):
            end = text.find("-->", index + 4)
            if end < 0:
                incomplete = (index, "formatting")
                break
            tag_ranges.append((index, end + 3))
            index = end + 3
            continue

        tag_end = _html_tag_end(text, index)
        if tag_end is None:
            if index + 1 < len(text) and (
                text[index + 1].isalpha() or text[index + 1] in "/!"
            ):
                incomplete = (index, "formatting")
            break
        token = text[index : tag_end + 1]
        if re.fullmatch(r"<https?://[^ >]+>", token, re.IGNORECASE):
            index = tag_end + 1
            continue
        match = re.match(r"<\s*(?P<closing>/)?\s*(?P<name>[A-Za-z][\w:-]*)", token)
        if match is None:
            index = tag_end + 1
            continue
        name = match.group("name").lower()
        if name not in _RICH_HTML_TAGS and not name.startswith("tg-"):
            index = tag_end + 1
            continue
        tag_ranges.append((index, tag_end + 1))
        if match.group("closing"):
            for stack_index in range(len(stack) - 1, -1, -1):
                if stack[stack_index][0] == name:
                    del stack[stack_index:]
                    break
        elif name not in _VOID_HTML_TAGS and not token.rstrip().endswith("/>"):
            stack.append((name, index))
        index = tag_end + 1

    candidates = [candidate for candidate in (incomplete,) if candidate is not None]
    candidates.extend((_rich_tag_kind(name, position)) for name, position in stack)
    candidate = min(candidates, key=lambda value: value[0]) if candidates else None
    return _mask_ranges(text, tag_ranges), candidate


def _html_tag_end(text: str, start: int) -> int | None:
    """Find a tag's closing angle bracket without stopping inside quotes."""
    quote: str | None = None
    for index in range(start + 1, len(text)):
        character = text[index]
        if quote is not None:
            if character == quote and not _is_escaped(text, index):
                quote = None
            continue
        if character in "\"'":
            quote = character
        elif character == ">":
            return index
    return None


def _rich_tag_kind(name: str, position: int) -> tuple[int, PendingRichKind]:
    if name in {"details", "summary"}:
        return position, "details"
    if name == "tg-map":
        return position, "map"
    if name == "tg-math-block":
        return position, "math"
    if name in {
        "tg-animation",
        "tg-audio",
        "tg-collage",
        "tg-document",
        "tg-photo",
        "tg-slideshow",
        "tg-video",
        "tg-voice-note",
    }:
        return position, "media"
    return position, "formatting"


def _incomplete_table(text: str, scan_text: str) -> tuple[int, PendingRichKind] | None:
    """Hold a possible header or only the unfinished final row of a table."""
    block_start = 0
    for separator in re.finditer(r"\n[ \t]*\n", scan_text):
        block_start = separator.end()
    block = scan_text[block_start:]
    lines = block.splitlines(keepends=True)
    if not lines:
        return None
    first = lines[0].rstrip("\r\n")
    if not _possible_table_header(first):
        return None
    if len(lines) == 1:
        return block_start, "table"

    delimiter = lines[1].rstrip("\r\n")
    if not _TABLE_DELIMITER.fullmatch(delimiter):
        if len(lines) == 2 and _possible_table_delimiter(delimiter):
            return block_start, "table"
        return None
    if not lines[1].endswith(("\n", "\r")):
        return block_start, "table"
    if len(lines) > 2 and not lines[-1].endswith(("\n", "\r")):
        return block_start + sum(len(line) for line in lines[:-1]), "table"
    return None


def _possible_table_header(line: str) -> bool:
    stripped = line.strip()
    structural = _MARKDOWN_SPOILER.sub("", stripped)
    return "|" in structural and (
        structural.startswith("|")
        or structural.endswith("|")
        or structural.count("|") > 1
    )


def _possible_table_delimiter(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and "-" in stripped and set(stripped) <= set("|:- \t")


def _incomplete_inline(text: str) -> tuple[int, PendingRichKind] | None:
    """Find the first inline opener whose closing delimiter has not arrived."""
    index = 0
    while index < len(text):
        if text[index].isspace() or _is_escaped(text, index):
            index += 1
            continue

        if text[index] == "`":
            end = index
            while end < len(text) and text[end] == "`":
                end += 1
            code_delimiter = text[index:end]
            closing = _find_unescaped(text, code_delimiter, end)
            if closing < 0:
                return index, "code"
            index = closing + len(code_delimiter)
            continue

        image = text.startswith("![", index)
        if image or text[index] == "[":
            opening = index + 1 if image else index
            bracket_end = _matching_delimiter(text, opening, "[", "]")
            if bracket_end is None:
                return index, "media" if image else "formatting"
            following = bracket_end + 1
            if following == len(text):
                return index, "media" if image else "formatting"
            if text[following] == "(":
                link_end = _matching_delimiter(text, following, "(", ")")
                if link_end is None:
                    return index, "media" if image else "formatting"
                index = link_end + 1
            else:
                index = following
            continue

        if text[index] == "$" and not text.startswith("$$", index):
            next_character = text[index + 1 : index + 2]
            if next_character and not next_character.isspace():
                closing = _find_unescaped(text, "$", index + 1)
                if closing >= 0:
                    index = closing + 1
                    continue
                if not next_character.isdigit():
                    return index, "math"

        inline_delimiter = next(
            (
                candidate
                for candidate in ("**", "__", "~~", "||", "==", "*", "_")
                if text.startswith(candidate, index)
            ),
            None,
        )
        if inline_delimiter is not None and _is_inline_opener(
            text, index, inline_delimiter
        ):
            closing = _find_unescaped(
                text, inline_delimiter, index + len(inline_delimiter)
            )
            if closing < 0:
                return index, "formatting"
            index = closing + len(inline_delimiter)
            continue
        index += 1
    return None


def _is_inline_opener(text: str, index: int, delimiter: str) -> bool:
    after = text[index + len(delimiter) : index + len(delimiter) + 1]
    if not after or after.isspace():
        return False
    before = text[index - 1 : index]
    if delimiter in {"_", "__"} and before.isalnum() and after.isalnum():
        return False
    if delimiter in {"*", "_"} and (not before or before in "\n\r") and after.isspace():
        return False
    return True


def _matching_delimiter(
    text: str, opening: int, opener: str, closer: str
) -> int | None:
    depth = 0
    for index in range(opening, len(text)):
        if _is_escaped(text, index):
            continue
        if text[index] == opener:
            depth += 1
        elif text[index] == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_unescaped(text: str, value: str, start: int) -> int:
    index = text.find(value, start)
    while index >= 0 and _is_escaped(text, index):
        index = text.find(value, index + len(value))
    return index


def _is_escaped(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return slashes % 2 == 1


def _mask_ranges(text: str, ranges: Sequence[tuple[int, int]]) -> str:
    if not ranges:
        return text
    characters = list(text)
    for start, end in ranges:
        for index in range(start, min(end, len(characters))):
            if characters[index] not in "\r\n":
                characters[index] = " "
    return "".join(characters)


def _disable_streaming_interactions(markdown: str) -> str:
    """Render interaction-shaped content inert until the terminal edit."""
    ranges = _complete_code_ranges(markdown)
    parts: list[str] = []
    cursor = 0
    for start, end in ranges:
        parts.append(_disable_interactions_segment(markdown[cursor:start]))
        parts.append(markdown[start:end])
        cursor = end
    parts.append(_disable_interactions_segment(markdown[cursor:]))
    return "".join(parts)


def _disable_interactions_segment(markdown: str) -> str:
    """Disable interactions in a source range known not to be code."""
    markdown = _replace_markdown_links(markdown)
    markdown = re.sub(r"<(https?://[^ >]+)>", lambda match: match.group(1), markdown)
    markdown = re.sub(r"<([^ <>@]+@[^ <>]+)>", lambda match: match.group(1), markdown)
    markdown = _ANCHOR.sub(lambda match: match.group("text"), markdown)
    markdown = _INTERACTIVE_INLINE_TAG.sub("", markdown)
    markdown = _DETAILS.sub(
        lambda match: (
            f"**{match.group('summary')}**\n\n{match.group('body').strip()}"
        ).rstrip(),
        markdown,
    )
    markdown = _MARKDOWN_SPOILER.sub(lambda match: match.group("text"), markdown)
    markdown = _HTML_SPOILER.sub(lambda match: match.group("text"), markdown)
    markdown = _MAP_BLOCK.sub("📍 _Map available when complete._", markdown)
    markdown = _MAP_SELF_CLOSING.sub("📍 _Map available when complete._", markdown)
    markdown = _MEDIA_BLOCK.sub("🖼️ _Media available when complete._", markdown)
    markdown = _MEDIA_SELF_CLOSING.sub("🖼️ _Media available when complete._", markdown)
    return _disable_untrusted_buttons(markdown)


def _complete_code_ranges(markdown: str) -> list[tuple[int, int]]:
    """Return code ranges whose literal contents must never be rewritten."""
    protected, _ = _fenced_code_ranges(markdown)
    protected.extend(match.span() for match in _HTML_CODE.finditer(markdown))
    protected.sort()
    inline: list[tuple[int, int]] = []

    index = 0
    protected_index = 0
    while index < len(markdown):
        while (
            protected_index < len(protected) and protected[protected_index][1] <= index
        ):
            protected_index += 1
        if (
            protected_index < len(protected)
            and protected[protected_index][0] <= index < protected[protected_index][1]
        ):
            index = protected[protected_index][1]
            continue
        if markdown[index] != "`" or _is_escaped(markdown, index):
            index += 1
            continue
        end = index
        while end < len(markdown) and markdown[end] == "`":
            end += 1
        delimiter = markdown[index:end]
        closing = _find_unescaped(markdown, delimiter, end)
        if closing < 0:
            index = end
            continue
        inline.append((index, closing + len(delimiter)))
        index = closing + len(delimiter)

    ranges = sorted((*protected, *inline))
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _replace_markdown_links(markdown: str) -> str:
    """Keep labels readable while withholding links and media actions."""
    result: list[str] = []
    cursor = 0
    index = 0
    while index < len(markdown):
        image = markdown.startswith("![", index)
        if (not image and markdown[index] != "[") or _is_escaped(markdown, index):
            index += 1
            continue
        opening = index + 1 if image else index
        bracket_end = _matching_delimiter(markdown, opening, "[", "]")
        if bracket_end is None or bracket_end + 1 >= len(markdown):
            index += 1
            continue
        if markdown[bracket_end + 1] != "(":
            index = bracket_end + 1
            continue
        link_end = _matching_delimiter(markdown, bracket_end + 1, "(", ")")
        if link_end is None:
            index += 1
            continue

        label = markdown[opening + 1 : bracket_end]
        target = markdown[bracket_end + 2 : link_end].strip().lower()
        if image and target.startswith("tg://emoji"):
            replacement = markdown[index : link_end + 1]
        elif image and target.startswith("tg://time"):
            replacement = label
        elif image:
            replacement = f"🖼️ _{label or 'Media'} available when complete._"
        else:
            replacement = label
        result.append(markdown[cursor:index])
        result.append(replacement)
        cursor = link_end + 1
        index = link_end + 1
    result.append(markdown[cursor:])
    return "".join(result)


def _disable_untrusted_buttons(markdown: str) -> str:
    """Keep model-authored buttons inert; Ariadne appends trusted controls later."""

    def disabled(match: re.Match[str]) -> str:
        style_match = re.search(
            r"\bstyle\s*=\s*([\"'])(danger|success|primary)\1",
            match.group("attributes"),
            re.IGNORECASE,
        )
        style = (
            f' style="{style_match.group(2).lower()}"'
            if style_match is not None
            else ""
        )
        return f'<tg-button type="disabled"{style}>{match.group("text")}</tg-button>'

    return _BUTTON.sub(disabled, markdown)


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
        disable_interactions: bool = False,
    ) -> Message:
        """Send one persistent Rich Markdown message."""
        return await self.send_payload(
            chat_id=chat_id,
            rich_message=rich_markdown(
                markdown,
                buttons=buttons,
                buttons_per_row=buttons_per_row,
                disable_interactions=disable_interactions,
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
        disable_interactions: bool = False,
    ) -> Message:
        """Replace an ordinary or Rich Message with Rich Markdown."""
        return await self.edit_payload(
            message,
            rich_markdown(
                markdown,
                buttons=buttons,
                buttons_per_row=buttons_per_row,
                disable_interactions=disable_interactions,
            ),
        )

    async def edit_by_id(
        self,
        *,
        chat_id: int,
        message_id: int,
        markdown: str,
        buttons: Sequence[RichButton] = (),
        buttons_per_row: int = 8,
        disable_interactions: bool = False,
    ) -> Message:
        """Edit a Rich Message when only its durable Telegram identity is known."""
        return await self.edit_payload_by_id(
            chat_id=chat_id,
            message_id=message_id,
            rich_message=rich_markdown(
                markdown,
                buttons=buttons,
                buttons_per_row=buttons_per_row,
                disable_interactions=disable_interactions,
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
