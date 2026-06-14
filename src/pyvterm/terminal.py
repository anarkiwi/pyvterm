"""High-level :class:`VectorTerminal` — the friendly front door of pyvterm.

This ties a :class:`~pyvterm.frame.FrameBuilder` to a
:class:`~pyvterm.transport.Transport` and exposes the same six operations as
``zvgFrame.c`` (open / set-rgb / set-clip / vector / send / close) plus a few
Pythonic conveniences (a pen for ``move_to``/``draw_to``, ``polyline``, a
``frame`` context manager, and context-manager support on the terminal
itself).

Example
-------
>>> from pyvterm import VectorTerminal
>>> with VectorTerminal(port="/dev/ttyACM0") as vt:  # doctest: +SKIP
...     with vt.frame():
...         vt.set_intensity(15)
...         vt.polyline([(-100, -100), (100, -100), (100, 100), (-100, 100)], closed=True)
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from . import protocol
from .frame import FrameBuilder
from .protocol import DEFAULT_BOUNDS, DVG_RENDER_QUALITY, Bounds
from .transport import DEFAULT_BAUDRATE, DEFAULT_PORT, SerialTransport, Transport

__all__ = ["VectorTerminal"]

Window = tuple[int, int, int, int]


class VectorTerminal:
    """Draw vector graphics on a pitrex/Vectrex over a USB-DVG serial link.

    Provide an explicit ``transport`` (e.g. a
    :class:`~pyvterm.transport.MemoryTransport` for testing), or a ``port`` to
    open a :class:`~pyvterm.transport.SerialTransport` automatically. Extra
    keyword arguments are forwarded to :class:`SerialTransport`.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        port: str | None = None,
        baudrate: int = DEFAULT_BAUDRATE,
        bounds: Bounds = DEFAULT_BOUNDS,
        clip_window: Window | None = None,
        quality: int = DVG_RENDER_QUALITY,
        monochrome: bool = False,
        **serial_kwargs: Any,
    ) -> None:
        if transport is None:
            transport = SerialTransport(port or DEFAULT_PORT, baudrate, **serial_kwargs)
        self.transport = transport
        self._frame = FrameBuilder(
            bounds=bounds,
            clip_window=clip_window,
            quality=quality,
            monochrome=monochrome,
        )
        self._pen: tuple[float, float] = (0.0, 0.0)

    @classmethod
    def open(
        cls, port: str = DEFAULT_PORT, baudrate: int = DEFAULT_BAUDRATE, **kwargs: Any
    ) -> VectorTerminal:
        """Open a serial terminal on ``port`` (``zvgFrameOpen``)."""
        return cls(port=port, baudrate=baudrate, **kwargs)

    # -- frame state -------------------------------------------------------

    @property
    def builder(self) -> FrameBuilder:
        """The underlying :class:`FrameBuilder` for the in-progress frame."""
        return self._frame

    def clear(self) -> VectorTerminal:
        """Discard the in-progress frame without sending it."""
        self._frame.reset()
        return self

    # -- colour / clipping -------------------------------------------------

    def set_rgb(self, r: int, g: int, b: int) -> VectorTerminal:
        """Set the colour of subsequent vectors (``zvgFrameSetRGB15``)."""
        self._frame.set_rgb(r, g, b)
        return self

    def set_intensity(self, level: int) -> VectorTerminal:
        """Set a monochrome brightness (``0`` = beam off ... ``15`` = brightest)."""
        self._frame.set_rgb(level, level, level)
        return self

    def set_clip_window(self, x_min: int, y_min: int, x_max: int, y_max: int) -> VectorTerminal:
        """Set the clip rectangle in host coordinates (``zvgFrameSetClipWin``)."""
        self._frame.set_clip_window(x_min, y_min, x_max, y_max)
        return self

    # -- drawing -----------------------------------------------------------

    def vector(self, x0: float, y0: float, x1: float, y1: float) -> VectorTerminal:
        """Draw a line from ``(x0, y0)`` to ``(x1, y1)`` (``zvgFrameVector``)."""
        self._frame.vector(x0, y0, x1, y1)
        self._pen = (x1, y1)
        return self

    def move_to(self, x: float, y: float) -> VectorTerminal:
        """Move the pen to ``(x, y)`` without drawing."""
        self._pen = (x, y)
        return self

    def draw_to(self, x: float, y: float) -> VectorTerminal:
        """Draw from the current pen position to ``(x, y)``."""
        self._frame.vector(self._pen[0], self._pen[1], x, y)
        self._pen = (x, y)
        return self

    def polyline(self, points: list[tuple[float, float]], closed: bool = False) -> VectorTerminal:
        """Draw a connected sequence of points; optionally close the loop."""
        self._frame.polyline(points, closed=closed)
        if points:
            self._pen = points[0] if closed else points[-1]
        return self

    # -- transmission ------------------------------------------------------

    def send_frame(self) -> bytes:
        """Serialise and transmit the frame, then reset for the next one.

        Returns the exact bytes written (handy for tests and dry runs).
        """
        data = self._frame.to_bytes()
        self.transport.write(data)
        self.transport.flush()
        self._frame.reset()
        return data

    @contextlib.contextmanager
    def frame(self) -> Iterator[VectorTerminal]:
        """Context manager that clears, yields for drawing, then sends.

        >>> with vt.frame():        # doctest: +SKIP
        ...     vt.set_intensity(15)
        ...     vt.draw_to(100, 0)
        """
        self.clear()
        yield self
        self.send_frame()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Send ``EXIT`` and close the transport (``zvgFrameClose``)."""
        with contextlib.suppress(Exception):
            self.transport.write(protocol.exit_command())
            self.transport.flush()
        self.transport.close()

    def __enter__(self) -> VectorTerminal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
