import sys
from pathlib import Path

import pytest

from ariadne.telegram import voice as voice_module
from ariadne.telegram.voice import CommandVoiceTranscriber, VoiceTranscriptionError


async def test_command_transcriber_uses_argv_without_a_shell(tmp_path: Path) -> None:
    audio = tmp_path / "note with spaces.ogg"
    audio.write_bytes(b"private")
    transcriber = CommandVoiceTranscriber(
        (
            sys.executable,
            "-c",
            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).name)",
            "{input}",
        ),
        timeout_seconds=5,
    )

    assert await transcriber.transcribe(audio) == "note with spaces.ogg"


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("raise SystemExit(2)", "transcriber failed"),
        ("print('')", "returned no speech"),
        ("import sys; sys.stdout.buffer.write(b'\\xff')", "not UTF-8"),
    ],
)
async def test_command_transcriber_reports_bounded_failures(
    tmp_path: Path, script: str, message: str
) -> None:
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"private")
    transcriber = CommandVoiceTranscriber(
        (sys.executable, "-c", script, "{input}"), timeout_seconds=5
    )

    with pytest.raises(VoiceTranscriptionError, match=message):
        await transcriber.transcribe(audio)


async def test_command_transcriber_rejects_oversized_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(voice_module, "MAX_TRANSCRIPT_BYTES", 4)
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"private")
    transcriber = CommandVoiceTranscriber(
        (sys.executable, "-c", "print('long transcript')", "{input}"),
        timeout_seconds=5,
    )

    with pytest.raises(VoiceTranscriptionError, match="too large"):
        await transcriber.transcribe(audio)


async def test_command_transcriber_times_out_and_terminates_process(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"private")
    transcriber = CommandVoiceTranscriber(
        (
            sys.executable,
            "-c",
            "import time; time.sleep(2)",
            "{input}",
        ),
        timeout_seconds=1,
    )

    with pytest.raises(VoiceTranscriptionError, match="timed out"):
        await transcriber.transcribe(audio)


def test_command_transcriber_requires_one_input_placeholder() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CommandVoiceTranscriber(("transcribe",), timeout_seconds=5)
    with pytest.raises(ValueError, match="exactly one"):
        CommandVoiceTranscriber(("transcribe", "{input}", "{input}"), timeout_seconds=5)
