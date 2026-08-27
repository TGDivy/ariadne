from typing import Any, cast

from telegram import Bot, Message

from ariadne.telegram.rich import (
    RICH_MESSAGE_LIMIT,
    RichBotAPI,
    RichButton,
    incoming_rich_markdown,
    rich_markdown,
    split_rich_markdown,
    streaming_rich_preview,
)


class FakeBot:
    def __init__(self, result: Message) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any], type[Message] | None]] = []

    async def do_api_request(
        self,
        endpoint: str,
        api_kwargs: dict[str, Any] | None = None,
        return_type: type[Message] | None = None,
        **_: object,
    ) -> Message:
        self.calls.append((endpoint, api_kwargs or {}, return_type))
        return self.result


def telegram_message(message_id: int = 90) -> Message:
    message = Message.de_json(
        {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": 7, "type": "private"},
        },
        None,
    )
    assert message is not None
    return message


async def test_send_rich_message_uses_ptbs_forward_compatible_api() -> None:
    result = telegram_message()
    bot = FakeBot(result)
    api = RichBotAPI(cast(Bot, bot))

    sent = await api.send(
        chat_id=7,
        markdown="## Native heading",
        reply_to_message_id=11,
        message_thread_id=3,
        buttons=(RichButton("Stop", "callback_data", "turn:stop", "danger"),),
    )

    assert sent is result
    assert bot.calls == [
        (
            "sendRichMessage",
            {
                "chat_id": 7,
                "rich_message": {
                    "markdown": (
                        "## Native heading\n\n"
                        '<tg-button-row align="right">\n'
                        '<tg-button type="callback_data" style="danger" '
                        'data="turn:stop">Stop</tg-button>\n'
                        "</tg-button-row>"
                    )
                },
                "reply_parameters": {
                    "message_id": 11,
                    "allow_sending_without_reply": True,
                },
                "message_thread_id": 3,
            },
            Message,
        )
    ]


async def test_edit_can_convert_an_ordinary_message_to_rich_content() -> None:
    message = telegram_message()
    bot = FakeBot(message)
    api = RichBotAPI(cast(Bot, bot))

    await api.edit(message, "| A | B |\n|---|---|\n| 1 | 2 |")

    assert bot.calls == [
        (
            "editMessageText",
            {
                "chat_id": 7,
                "message_id": 90,
                "rich_message": {"markdown": "| A | B |\n|---|---|\n| 1 | 2 |"},
            },
            Message,
        )
    ]


def test_button_attributes_and_text_are_escaped() -> None:
    payload = rich_markdown(
        "Question",
        buttons=(RichButton("A & B", "callback_data", 'choice:"1"'),),
    )

    assert "A &amp; B" in payload["markdown"]
    assert "choice:&quot;1&quot;" in payload["markdown"]


def test_buttons_can_be_split_into_readable_rows() -> None:
    payload = rich_markdown(
        "Choose",
        buttons=tuple(
            RichButton(str(index), "callback_data", f"choice:{index}")
            for index in range(5)
        ),
        buttons_per_row=2,
    )

    assert payload["markdown"].count("<tg-button-row") == 3
    assert payload["markdown"].count("<tg-button type=") == 5


def test_partial_code_fence_is_closed_before_a_button_row() -> None:
    payload = rich_markdown(
        "```python\nprint('still streaming')",
        buttons=(RichButton("Stop", "callback_data", "turn:stop"),),
    )

    assert payload["markdown"].startswith(
        "```python\nprint('still streaming')\n```\n\n<tg-button-row"
    )


def test_streaming_preview_holds_only_structurally_incomplete_tails() -> None:
    cases = [
        ("Intro\n\n```python\nprint(1)", "Intro", "code"),
        ("A **formatted", "A", "formatting"),
        ("See [the docs](https://exam", "See", "formatting"),
        ("Value: $x + y", "Value:", "math"),
        (
            "Before\n\n<details><summary>Logs</summary>Still loading",
            "Before",
            "details",
        ),
        (
            'Before\n\n<tg-map latitude="1" longitude="2">Somewhere',
            "Before",
            "map",
        ),
    ]

    for source, expected, pending in cases:
        preview = streaming_rich_preview(source)
        assert preview.markdown == expected
        assert preview.pending == pending


def test_streaming_table_commits_complete_rows_without_exposing_partial_ones() -> None:
    header = "| Option | Tradeoff |\n|---|---|\n"

    assert streaming_rich_preview("| Option |").markdown == ""
    assert streaming_rich_preview(header).markdown == header.rstrip()
    preview = streaming_rich_preview(header + "| Fast | Less thor")
    assert preview.markdown == header.rstrip()
    assert preview.activity == "Building a table…"
    assert streaming_rich_preview(header + "| Fast | Less thorough |\n").markdown == (
        header + "| Fast | Less thorough |"
    )


def test_streaming_interactions_are_inert_but_trusted_stop_stays_live() -> None:
    source = (
        "See [docs](https://example.com), @iris.\n\n"
        "![Chart](https://example.com/chart.png)\n\n"
        '<tg-map latitude="1" longitude="2">Here</tg-map>\n\n'
        '<tg-button type="callback_data" data="settings:models">'
        "Change</tg-button>"
    )

    preview = streaming_rich_preview(source)
    payload = rich_markdown(
        preview.markdown,
        buttons=(RichButton("Stop", "callback_data", "turn:stop"),),
        disable_interactions=True,
    )

    assert payload["skip_entity_detection"] is True
    assert "https://example.com" not in payload["markdown"]
    assert "Chart available when complete" in payload["markdown"]
    assert "Map available when complete" in payload["markdown"]
    assert 'data="settings:models"' not in payload["markdown"]
    assert '<tg-button type="disabled">Change</tg-button>' in payload["markdown"]
    assert 'data="turn:stop"' in payload["markdown"]


def test_streaming_keeps_code_literal_and_flattens_other_reveal_controls() -> None:
    source = (
        "```python\n"
        'example = "[docs](https://example.com) || unchanged ||"\n'
        "```\n\n"
        "A ||spoiler|| and "
        "<details><summary>More</summary>Hidden text</details>"
    )

    preview = streaming_rich_preview(source)

    assert "[docs](https://example.com) || unchanged ||" in preview.markdown
    assert "A spoiler and **More**\n\nHidden text" in preview.markdown
    assert "<details>" not in preview.markdown


def test_terminal_links_and_media_activate_without_trusting_callbacks() -> None:
    source = (
        "[docs](https://example.com)\n\n"
        "![Chart](https://example.com/chart.png)\n\n"
        '<tg-map latitude="1" longitude="2">Here</tg-map>\n\n'
        '<tg-button type="callback_data" data="settings:models">'
        "Change</tg-button>"
    )

    payload = rich_markdown(source)

    assert "skip_entity_detection" not in payload
    assert "[docs](https://example.com)" in payload["markdown"]
    assert "![Chart](https://example.com/chart.png)" in payload["markdown"]
    assert "<tg-map" in payload["markdown"]
    assert 'data="settings:models"' not in payload["markdown"]
    assert '<tg-button type="disabled">Change</tg-button>' in payload["markdown"]


def test_classic_limit_no_longer_discards_formatting() -> None:
    response = "## Heading\n\n" + "x" * 5_000

    assert split_rich_markdown(response) == [response]


def test_oversized_rich_messages_split_at_complete_blocks() -> None:
    first = "a" * 20
    second = "b" * 20

    assert split_rich_markdown(f"{first}\n\n{second}", limit=25) == [first, second]


def test_oversized_code_blocks_reopen_the_fence() -> None:
    response = "```python\n" + "x" * 40 + "\n```"

    chunks = split_rich_markdown(response, limit=30)

    assert len(chunks) > 1
    assert all(chunk.startswith("```python\n") for chunk in chunks)
    assert all(chunk.endswith("\n```") for chunk in chunks)
    assert all(len(chunk) <= 30 for chunk in chunks)


def test_oversized_partial_code_blocks_are_closed_before_splitting() -> None:
    response = "```python\n" + "x" * 40

    chunks = split_rich_markdown(response, limit=30)

    assert len(chunks) > 1
    assert all(chunk.startswith("```python\n") for chunk in chunks)
    assert all(chunk.endswith("\n```") for chunk in chunks)


def test_incoming_rich_messages_are_available_before_native_ptb_support() -> None:
    message = Message.de_json(
        {
            "message_id": 11,
            "date": 1,
            "chat": {"id": 7, "type": "private"},
            "rich_message": {
                "blocks": [
                    {"type": "heading", "size": 2, "text": "Direction"},
                    {
                        "type": "paragraph",
                        "text": [
                            "Use ",
                            {"type": "bold", "text": "Rich Messages"},
                            ".",
                        ],
                    },
                ]
            },
        },
        None,
    )
    assert message is not None

    assert incoming_rich_markdown(message) == ("## Direction\n\nUse **Rich Messages**.")


def test_incoming_tables_tasks_quotes_and_media_keep_their_meaning() -> None:
    message = Message.de_json(
        {
            "message_id": 11,
            "date": 1,
            "chat": {"id": 7, "type": "private"},
            "rich_message": {
                "blocks": [
                    {
                        "type": "table",
                        "caption": "Deployment options",
                        "cells": [
                            [{"text": "Environment"}, {"text": "Risk"}],
                            [{"text": "Production"}, {"text": "High | urgent"}],
                        ],
                    },
                    {
                        "type": "list",
                        "items": [
                            {
                                "label": "•",
                                "has_checkbox": True,
                                "is_checked": True,
                                "blocks": [
                                    {"type": "paragraph", "text": "Tests passed"}
                                ],
                            }
                        ],
                    },
                    {
                        "type": "blockquote",
                        "blocks": [{"type": "paragraph", "text": "Ship it"}],
                        "credit": "Release lead",
                    },
                    {
                        "type": "document",
                        "document": {"file_name": "report.pdf"},
                        "caption": {"text": "Full results"},
                    },
                ]
            },
        },
        None,
    )
    assert message is not None

    assert incoming_rich_markdown(message) == (
        "Deployment options\n\n"
        "| Environment | Risk |\n"
        "| --- | --- |\n"
        "| Production | High \\| urgent |\n\n"
        "- [x] Tests passed\n\n"
        "> Ship it\n"
        "> — Release lead\n\n"
        "[Document: report.pdf — Full results]"
    )


def test_rich_message_limit_matches_telegram_bot_api() -> None:
    assert RICH_MESSAGE_LIMIT == 32_768
