"""Prepare and explicitly approve Telegram file delivery."""

import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

PENDING_DELIVERY_TTL_SECONDS = 15 * 60


class FileDeliveryError(ValueError):
    """Raised when a requested file delivery cannot be safely staged."""


@dataclass(frozen=True, slots=True)
class StagedFile:
    path: Path
    size_bytes: int


class FileDelivery:
    """Durable, short-lived staging shared by the MCP tool and Telegram bot."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or (
            Path(tempfile.gettempdir()) / "ariadne-delivery"
        )
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def stage(self, raw_paths: list[str]) -> tuple[str, tuple[StagedFile, ...]]:
        if not raw_paths:
            raise FileDeliveryError("At least one file is required.")
        home = Path.home().resolve()
        files: list[StagedFile] = []
        for raw_path in raw_paths:
            path = Path(raw_path).expanduser().resolve()
            if not path.is_relative_to(home):
                raise FileDeliveryError(
                    f"File is outside the allowed home directory: {raw_path}"
                )
            if not path.is_file() or not os.access(path, os.R_OK):
                raise FileDeliveryError(
                    f"File is not a readable regular file: {raw_path}"
                )
            files.append(StagedFile(path=path, size_bytes=path.stat().st_size))

        approval_id = secrets.token_urlsafe(18)
        record = {
            "expires_at": time.time() + PENDING_DELIVERY_TTL_SECONDS,
            "files": [
                {"path": str(file.path), "size_bytes": file.size_bytes}
                for file in files
            ],
        }
        record_path = self._record_path(approval_id)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        record_path.chmod(0o600)
        return approval_id, tuple(files)

    async def approve(
        self, approval_id: str, *, token: str, chat_id: int
    ) -> tuple[StagedFile, ...]:
        record_path = self._record_path(approval_id)
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FileDeliveryError(
                "That file-delivery approval is unavailable."
            ) from error
        if not isinstance(record, dict) or record.get("expires_at", 0) < time.time():
            record_path.unlink(missing_ok=True)
            raise FileDeliveryError("That file-delivery approval has expired.")

        files = tuple(
            StagedFile(path=Path(item["path"]), size_bytes=int(item["size_bytes"]))
            for item in record.get("files", [])
            if isinstance(item, dict)
        )
        if not files:
            raise FileDeliveryError("That file-delivery approval contains no files.")
        for file in files:
            if not file.path.is_file() or not os.access(file.path, os.R_OK):
                raise FileDeliveryError(
                    f"The staged file is no longer readable: {file.path.name}"
                )

        try:
            async with Bot(token) as bot:
                for file in files:
                    with file.path.open("rb") as document:
                        await bot.send_document(chat_id=chat_id, document=document)
        except Exception as error:
            raise FileDeliveryError(
                "Telegram could not deliver the staged files."
            ) from error
        record_path.unlink(missing_ok=True)
        return files

    def reject(self, approval_id: str) -> None:
        """Discard a staged batch without sending any files."""
        self._record_path(approval_id).unlink(missing_ok=True)

    async def request_approval(
        self,
        approval_id: str,
        files: tuple[StagedFile, ...],
        *,
        token: str,
        chat_id: int,
    ) -> None:
        """Send the explicit Telegram approval card for one staged batch."""
        file_list = "\n".join(
            f"• {file.path.name} ({file.size_bytes:,} bytes)" for file in files
        )
        text = f"Ready to send these files:\n\n{file_list}"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Approve", callback_data=f"file-delivery:approve:{approval_id}"
                    ),
                    InlineKeyboardButton(
                        "Reject", callback_data=f"file-delivery:reject:{approval_id}"
                    ),
                ]
            ]
        )
        try:
            async with Bot(token) as bot:
                await bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=keyboard
                )
        except Exception as error:
            self.reject(approval_id)
            raise FileDeliveryError(
                "Telegram could not send the file-delivery approval request."
            ) from error

    def _record_path(self, approval_id: str) -> Path:
        allowed = "-_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        if not approval_id or any(char not in allowed for char in approval_id):
            raise FileDeliveryError("Invalid file-delivery approval ID.")
        return self._directory / f"{approval_id}.json"
