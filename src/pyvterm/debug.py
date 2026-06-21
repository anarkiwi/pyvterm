"""Generic ``--debug`` telemetry for pyvterm senders.

:class:`DebugReporter` samples a running :class:`~pyvterm.terminal.VectorTerminal`
once per frame and prints a periodic one-line summary: the outbound I/O rate, and
the min/mean/max of the per-frame **vector count** and **draw time** the receiver
reports in its v2 sync record (so the vector/draw figures need a v2 device; the
I/O rate works with any transport). It is deliberately tiny and pure-stdlib so
examples can wire it in with two lines.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import TYPE_CHECKING, Callable, TextIO

if TYPE_CHECKING:
    from .terminal import VectorTerminal

__all__ = ["DebugReporter", "add_debug_argument", "reporter_for"]


def add_debug_argument(
    parser: argparse._ActionsContainer, name: str = "--debug"
) -> argparse.Action:
    """Add the standard optional ``--debug [SECS]`` flag to ``parser``.

    Absent → ``None`` (off); bare ``--debug`` → ``1`` (one line/second);
    ``--debug N`` → every ``N`` seconds.
    """
    return parser.add_argument(
        name,
        nargs="?",
        type=int,
        const=1,
        default=None,
        metavar="SECS",
        help="print telemetry every SECS seconds (default 1): outbound I/O rate "
        "and the min/mean/max vector count and draw time the device reports",
    )


def reporter_for(
    terminal: VectorTerminal, period: int | float | None, **kwargs: object
) -> DebugReporter | None:
    """Build a :class:`DebugReporter` for ``period`` (from ``--debug``), or ``None``."""
    if not period:
        return None
    return DebugReporter(terminal, float(period), **kwargs)  # type: ignore[arg-type]


def _min_mean_max(values: list[int]) -> str:
    if not values:
        return "-/-/-"
    return f"{min(values)}/{sum(values) / len(values):.1f}/{max(values)}"


class DebugReporter:
    """Accumulate per-frame telemetry and emit a line every ``period`` seconds.

    Call :meth:`tick` once per frame (after sending). Every ``period`` seconds it
    prints the I/O rate over the interval plus the min/mean/max vector count and
    draw time reported by the device since the previous line, then resets.
    """

    def __init__(
        self,
        terminal: VectorTerminal,
        period: float = 1.0,
        *,
        out: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.terminal = terminal
        self.period = max(0.001, period)
        self.out = sys.stderr if out is None else out
        self._clock = clock
        self._last_timing: object = None
        self._reset(self._clock())

    def _reset(self, now: float) -> None:
        tr = self.terminal.transport
        self._t0 = now
        self._bytes0 = getattr(tr, "bytes_sent", 0)
        self._frames0 = getattr(tr, "frames_sent", 0)
        self._skipped0 = getattr(tr, "frames_skipped", 0)
        self._vectors: list[int] = []
        self._draws: list[int] = []

    def tick(self) -> None:
        """Sample the latest device report (if new) and emit a line when due."""
        timing = self.terminal.last_timing
        if timing is not None and timing is not self._last_timing:
            self._last_timing = timing
            self._vectors.append(timing.vectors)
            self._draws.append(timing.draw_us)
        now = self._clock()
        if now - self._t0 >= self.period:
            self.report(now)
            self._reset(now)

    def report(self, now: float | None = None) -> None:
        """Print one telemetry line for the interval since the last reset."""
        now = self._clock() if now is None else now
        tr = self.terminal.transport
        elapsed = now - self._t0
        bytes_delta = getattr(tr, "bytes_sent", 0) - self._bytes0
        frames = getattr(tr, "frames_sent", 0) - self._frames0
        skipped = getattr(tr, "frames_skipped", 0) - self._skipped0
        bps = (bytes_delta * 8 / elapsed) if elapsed > 0 else 0.0
        print(
            f"[debug] {elapsed:4.1f}s  io={bps:,.0f} bps  frames={frames} skipped={skipped}  "
            f"vectors min/mean/max={_min_mean_max(self._vectors)}  "
            f"draw_us min/mean/max={_min_mean_max(self._draws)}",
            file=self.out,
            flush=True,
        )
