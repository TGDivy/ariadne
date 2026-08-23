from io import StringIO

import pytest

from ariadne.scripts.progress import ProgressBar


class TerminalBuffer(StringIO):
    def isatty(self) -> bool:
        return True


def test_progress_bar_renders_start_completion_rate_and_eta() -> None:
    stream = TerminalBuffer()
    times = iter((0.0, 0.0, 2.0))
    progress = ProgressBar("Scanning", stream=stream, clock=lambda: next(times))

    progress.update(0, 4)
    progress.update(4, 4)

    rendered = stream.getvalue()
    assert "Scanning [----------------------------] 0/4 0.0/s ETA --:--" in rendered
    assert "Scanning [############################] 4/4 2.0/s ETA 00:00\n" in rendered


def test_progress_bar_stays_quiet_when_output_is_redirected() -> None:
    stream = StringIO()

    with ProgressBar("Scanning", stream=stream) as progress:
        progress.update(0, 4)
        progress.update(4, 4)

    assert stream.getvalue() == ""


def test_progress_bar_finishes_an_incomplete_line_after_an_error() -> None:
    stream = TerminalBuffer()
    times = iter((0.0, 1.0))

    with pytest.raises(RuntimeError, match="move failed"):
        with ProgressBar(
            "Applying", stream=stream, clock=lambda: next(times)
        ) as progress:
            progress.update(1, 4)
            raise RuntimeError("move failed")

    assert stream.getvalue().endswith("\n")
