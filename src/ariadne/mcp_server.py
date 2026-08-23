"""Local FastMCP capabilities for Ariadne's Codex conversation."""

import os
import subprocess
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from telegram import Bot, ReplyParameters
from telegram.constants import ParseMode, ReactionEmoji
from telegram.error import BadRequest, TelegramError

from .codex import MCP_TOOLS
from .file_delivery import FileDelivery, FileDeliveryError
from .mail import Importance, SuggestedAction, record_current_mail_decision
from .telegram_format import split_for_telegram, telegram_messages

mcp = FastMCP(
    "Ariadne",
    instructions=(
        "Local runtime inspection, speaking in Telegram, "
        "and explicitly approved Telegram delivery."
    ),
    version="0.1.0",
    strict_input_validation=True,
)

VARIATION_SELECTOR = "\ufe0f"
REACTIONS = {
    emoji.value.replace(VARIATION_SELECTOR, ""): emoji.value for emoji in ReactionEmoji
}


def _git_status(vault: Path) -> dict[str, Any] | None:
    if not (vault / ".git").exists():
        return None
    try:
        branch = subprocess.run(
            ["git", "-C", str(vault), "branch", "--show-current"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain"],
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {"root": str(vault), "branch": branch or None, "is_dirty": dirty}


def _process(pid: int) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": pid, "name": None, "parent_pid": None}
    try:
        proc = Path("/proc") / str(pid)
        result["name"] = (proc / "comm").read_text().strip()
        result["parent_pid"] = int((proc / "stat").read_text().split()[3])
    except (OSError, IndexError, ValueError):
        pass
    return result


@mcp.tool
def runtime_status() -> dict[str, Any]:
    """Inspect Ariadne's current local runtime and Git workspace.

    Secrets and environment values are never returned.
    """
    vault = Path(os.environ.get("ARIADNE_VAULT", Path.cwd())).resolve()
    return {
        "server": {"name": "ariadne", "version": "0.1.0"},
        "cwd": str(Path.cwd()),
        "vault": str(vault),
        "git": _git_status(vault),
        "process": {
            "current": _process(os.getpid()),
            "parent": _process(os.getppid()),
        },
        "capabilities": list(MCP_TOOLS),
    }


def _telegram_chat() -> tuple[str, int]:
    """Return the credentials for the one private chat Ariadne speaks in."""
    try:
        return os.environ["TELEGRAM_BOT_TOKEN"], int(
            os.environ["TELEGRAM_ALLOWED_USER_ID"]
        )
    except (KeyError, ValueError) as error:
        raise ToolError("Telegram is not reachable from this runtime.") from error


async def _send_chunks(
    bot: Bot,
    chat_id: int,
    chunks: list[str],
    parse_mode: ParseMode | None,
    reply_to: ReplyParameters | None,
) -> list[int]:
    sent: list[int] = []
    for chunk in chunks:
        message = await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=parse_mode,
            reply_parameters=reply_to if not sent else None,
        )
        sent.append(message.message_id)
    return sent


@mcp.tool
async def send_message(text: str, reply_to_message_id: int | None = None) -> list[int]:
    """Say this in Telegram now, without waiting for the turn to end.

    The message stays in the chat as your own. Write it as you would any other
    message; Ariadne handles Markdown rendering and Telegram's length limit.
    """
    if not text.strip():
        raise ToolError("A message needs something to say.")

    token, chat_id = _telegram_chat()
    chunks, is_html = telegram_messages(text)
    reply_to = (
        ReplyParameters(reply_to_message_id, allow_sending_without_reply=True)
        if reply_to_message_id is not None
        else None
    )
    try:
        async with Bot(token) as bot:
            try:
                return await _send_chunks(
                    bot,
                    chat_id,
                    chunks,
                    ParseMode.HTML if is_html else None,
                    reply_to,
                )
            except BadRequest:
                if not is_html:
                    raise
                return await _send_chunks(
                    bot, chat_id, split_for_telegram(text), None, reply_to
                )
    except TelegramError as error:
        raise ToolError("Telegram could not deliver that message.") from error


@mcp.tool
async def react(message_id: int, reaction: str) -> None:
    """React to one Telegram message with a single emoji.

    Passing an empty reaction removes the one already there.
    """
    emoji = REACTIONS.get(reaction.replace(VARIATION_SELECTOR, ""))
    if reaction and emoji is None:
        raise ToolError(f"Telegram has no {reaction} reaction.")

    token, chat_id = _telegram_chat()
    try:
        async with Bot(token) as bot:
            await bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=emoji
            )
    except TelegramError as error:
        raise ToolError("Telegram could not add that reaction.") from error


@mcp.tool
async def prepare_files(paths: list[str]) -> dict[str, Any]:
    """Stage files under the user's home directory for explicit Telegram approval.

    This tool does not upload files. It sends an explicit Telegram approval card
    with Approve and Reject buttons for the exact staged batch.
    """
    try:
        approval_id, files = FileDelivery().stage(paths)
    except FileDeliveryError as error:
        raise ToolError(str(error)) from error
    try:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
        await FileDelivery().request_approval(
            approval_id, files, token=token, chat_id=chat_id
        )
    except (FileDeliveryError, KeyError, ValueError) as error:
        raise ToolError(str(error)) from error
    return {
        "approval_id": approval_id,
        "expires_in_seconds": 900,
        "approval_requested": True,
        "files": [
            {
                "path": str(file.path),
                "filename": file.path.name,
                "size_bytes": file.size_bytes,
            }
            for file in files
        ],
    }


@mcp.tool
def triage_current_mail(
    classification: str,
    importance: Importance,
    suggested_action: SuggestedAction,
    draft_reply: str | None = None,
) -> dict[str, str]:
    """Record the decision for this mail event and request a safe mailbox action.

    This capability exists only in mail turns. It can keep or flag the message,
    or move it to one of Ariadne's five configured filing folders. A draft reply
    is recorded for the user; this tool never sends email.
    """
    try:
        return record_current_mail_decision(
            classification, importance, suggested_action, draft_reply
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


def main() -> None:
    """Run the local server over FastMCP's default stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
