"""Render a small Markdown subset as Telegram-supported HTML."""

from html import escape
from re import fullmatch
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from markdown_it.token import Token

PARSER = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")


def render_telegram_html(markdown: str) -> str:
    """Return safe Telegram HTML for an Ariadne Markdown response."""
    parts: list[str] = []
    list_markers: list[int | None] = []
    list_item_depth = 0

    for token in PARSER.parse(markdown):
        if token.type == "heading_open":
            parts.append("<b>")
        elif token.type == "heading_close":
            parts.append("</b>\n\n")
        elif token.type == "paragraph_close":
            parts.append("\n" if list_item_depth else "\n\n")
        elif token.type == "bullet_list_open":
            list_markers.append(None)
        elif token.type == "ordered_list_open":
            start = token.attrGet("start")
            list_markers.append(int(start) if start is not None else 1)
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_markers.pop()
            if not list_markers:
                parts.append("\n")
        elif token.type == "list_item_open":
            list_item_depth += 1
            marker = list_markers[-1]
            if marker is None:
                prefix = "• "
            else:
                prefix = f"{marker}. "
                list_markers[-1] = marker + 1
            parts.append(f"{'  ' * (len(list_markers) - 1)}{prefix}")
        elif token.type == "list_item_close":
            list_item_depth -= 1
            if not parts or not parts[-1].endswith("\n"):
                parts.append("\n")
        elif token.type == "blockquote_open":
            parts.append("<blockquote>")
        elif token.type == "blockquote_close":
            parts.append("</blockquote>\n\n")
        elif token.type in {"fence", "code_block"}:
            parts.append(_render_code_block(token))
        elif token.type == "hr":
            parts.append("────────\n")
        elif token.type == "inline":
            parts.append(_render_inline(token.children or []))
        elif token.type == "html_block":
            parts.append(escape(token.content))

    return "".join(parts).strip()


def _render_inline(tokens: list[Token]) -> str:
    """Render inline Markdown tokens with Telegram's allowed HTML tags."""
    parts: list[str] = []
    rendered_links: list[bool] = []

    tags = {
        "strong_open": "<b>",
        "strong_close": "</b>",
        "em_open": "<i>",
        "em_close": "</i>",
        "s_open": "<s>",
        "s_close": "</s>",
    }

    for token in tokens:
        if token.type == "text":
            parts.append(escape(token.content))
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif token.type == "code_inline":
            parts.append(f"<code>{escape(token.content)}</code>")
        elif token.type in tags:
            parts.append(tags[token.type])
        elif token.type == "link_open":
            href = token.attrGet("href")
            if isinstance(href, str) and _is_supported_url(href):
                rendered_links.append(True)
                parts.append(f'<a href="{escape(href, quote=True)}">')
            else:
                rendered_links.append(False)
        elif token.type == "link_close":
            if rendered_links.pop():
                parts.append("</a>")
        elif token.type == "image":
            parts.append(escape(token.content))
        elif token.type == "html_inline":
            parts.append(escape(token.content))

    return "".join(parts)


def _render_code_block(token: Token) -> str:
    """Render a fenced or indented code block without allowing arbitrary HTML."""
    content = escape(token.content)
    language = token.info.strip().split(maxsplit=1)[0] if token.info.strip() else ""
    if language and fullmatch(r"[A-Za-z0-9_+-]+", language):
        return f'<pre><code class="language-{language}">{content}</code></pre>\n\n'
    return f"<pre>{content}</pre>\n\n"


def _is_supported_url(url: str) -> bool:
    """Return whether Telegram can safely render this link as an anchor."""
    return urlparse(url).scheme.lower() in {"http", "https"}
