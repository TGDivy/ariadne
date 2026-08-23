"""Tiny terminal progress reporting shared by operator scripts."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Self, TextIO


class ProgressBar:
    """Render a throttled progress line only when attached to a terminal."""

    def __init__(
        self,
        label: str,
        *,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool | None = None,
        min_interval: float = 0.1,
    ) -> None:
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.min_interval = min_interval
        self.started = clock()
        self.last_rendered: float | None = None
        self.line_open = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def update(self, done: int, total: int) -> None:
        if not self.enabled or total <= 0:
            return
        done = min(max(done, 0), total)
        now = self.clock()
        if (
            0 < done < total
            and self.last_rendered is not None
            and now - self.last_rendered < self.min_interval
        ):
            return

        elapsed = max(now - self.started, 0.001)
        rate = done / elapsed
        remaining = (total - done) / rate if rate else None
        eta = (
            f"{int(remaining // 60):02d}:{int(remaining % 60):02d}"
            if remaining is not None
            else "--:--"
        )
        width = 28
        filled = round(width * done / total)
        bar = "#" * filled + "-" * (width - filled)
        ending = "\n" if done == total else ""
        self.stream.write(
            f"\r{self.label} [{bar}] {done}/{total} {rate:.1f}/s ETA {eta}{ending}"
        )
        self.stream.flush()
        self.last_rendered = now
        self.line_open = done < total

    def close(self) -> None:
        if self.enabled and self.line_open:
            self.stream.write("\n")
            self.stream.flush()
            self.line_open = False
