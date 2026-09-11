"""Configurable, local command boundary for Telegram voice transcription."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Protocol

MAX_TRANSCRIPT_BYTES = 256 * 1024


class VoiceTranscriptionError(RuntimeError):
    """A voice note could not be transcribed into bounded text."""


class VoiceTranscriber(Protocol):
    """Translate one private local audio file into plain text."""

    async def transcribe(self, path: Path) -> str:
        """Return a bounded transcript for the supplied audio file."""


class CommandVoiceTranscriber:
    """Run an argv-only local transcriber that writes its transcript to stdout."""

    def __init__(self, command: tuple[str, ...], *, timeout_seconds: int) -> None:
        if sum(argument.count("{input}") for argument in command) != 1:
            raise ValueError("Voice command needs exactly one {input} placeholder.")
        if timeout_seconds <= 0:
            raise ValueError("Voice transcription timeout must be positive.")
        self._command = command
        self._timeout_seconds = timeout_seconds

    async def transcribe(self, path: Path) -> str:
        """Execute without a shell and interpret bounded stdout as the transcript."""
        arguments = tuple(
            argument.replace("{input}", str(path)) for argument in self._command
        )
        with tempfile.TemporaryFile() as output:
            try:
                process = await asyncio.create_subprocess_exec(
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=output,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as error:
                raise VoiceTranscriptionError(
                    "The voice transcriber is unavailable."
                ) from error
            try:
                await asyncio.wait_for(process.wait(), timeout=self._timeout_seconds)
            except TimeoutError as error:
                process.kill()
                await process.wait()
                raise VoiceTranscriptionError(
                    "Voice transcription timed out."
                ) from error
            output.seek(0)
            stdout = output.read(MAX_TRANSCRIPT_BYTES + 1)
        if process.returncode != 0:
            raise VoiceTranscriptionError("The voice transcriber failed.")
        if len(stdout) > MAX_TRANSCRIPT_BYTES:
            raise VoiceTranscriptionError("The voice transcript is too large.")
        try:
            transcript = stdout.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise VoiceTranscriptionError(
                "The voice transcript is not UTF-8."
            ) from error
        if not transcript:
            raise VoiceTranscriptionError("The voice transcriber returned no speech.")
        return transcript
