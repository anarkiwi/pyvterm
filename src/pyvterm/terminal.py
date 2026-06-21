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
>>> with VectorTerminal(port="/dev/ttyUSB0") as vt:  # doctest: +SKIP
...     with vt.frame():
...         vt.set_intensity(15)
...         vt.polyline([(-100, -100), (100, -100), (100, 100), (-100, 100)], closed=True)
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import Any

from . import ext, protocol
from .frame import FrameBuilder
from .protocol import DEFAULT_BOUNDS, DVG_RENDER_QUALITY, Bounds, Capability, HelloDescriptor
from .transport import BAUD_AUTO, DEFAULT_PORT, FrameTiming, SerialTransport, Transport

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
        baudrate: int | str = BAUD_AUTO,
        bounds: Bounds = DEFAULT_BOUNDS,
        clip_window: Window | None = None,
        quality: int = DVG_RENDER_QUALITY,
        monochrome: bool = False,
        suppress_duplicates: bool = False,
        **serial_kwargs: Any,
    ) -> None:
        if transport is None:
            transport = SerialTransport(port or DEFAULT_PORT, baudrate, **serial_kwargs)
        self.transport = transport
        self.bounds = bounds
        self._frame = FrameBuilder(
            bounds=bounds,
            clip_window=clip_window,
            quality=quality,
            monochrome=monochrome,
        )
        self._pen: tuple[float, float] = (0.0, 0.0)
        #: When set, :meth:`send_frame` skips transmitting a frame byte-identical
        #: to the last one sent — the cheapest temporal delta (see §6 of
        #: ``docs/PROTOCOL-EXTENSIONS.md``).
        self.suppress_duplicates = suppress_duplicates
        self._last_sent: bytes | None = None
        #: Diagnostics: frames suppressed because they matched the previous one.
        self.frames_suppressed = 0
        #: Capabilities advertised by the device, populated by :meth:`negotiate`.
        self.capabilities: HelloDescriptor | None = None

    @classmethod
    def open(
        cls, port: str = DEFAULT_PORT, baudrate: int | str = BAUD_AUTO, **kwargs: Any
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

    # -- negotiation -------------------------------------------------------

    def negotiate(self) -> HelloDescriptor | None:
        """Probe the device and record its v2 capabilities (or ``None``).

        Safe to call against any device: a plain USB-DVG (or nothing) leaves
        :attr:`capabilities` as ``None`` and the terminal stays on the base
        protocol.
        """
        self.capabilities = self.transport.probe_capabilities()
        return self.capabilities

    def supports(self, capability: Capability) -> bool:
        """True if the negotiated device advertised ``capability``."""
        return self.capabilities is not None and self.capabilities.supports(capability)

    # -- timing / keepalive ------------------------------------------------

    @property
    def last_timing(self) -> FrameTiming | None:
        """The receiver's most recent frame-draw timing, or ``None``.

        Populated from the device's sync reply (vekterm v2). Use
        :attr:`FrameTiming.draw_us` to throttle the send rate to scene
        complexity, e.g. ``time.sleep(max(0, target_dt - draw_us / 1e6))``.
        """
        return getattr(self.transport, "last_timing", None)

    def pace(self, fps: float | None) -> None:
        """Sleep to honour a frame rate, or auto-adapt to the device when ``fps``
        is ``None``.

        A positive ``fps`` caps the rate at ``1/fps`` seconds per frame. ``None``
        (or ``<= 0``) means *auto*: when flow control is active the send already
        blocks until the device signals it is ready, so the loop is paced at the
        device's true rate and no extra sleep is added; when there is no
        back-pressure (a USB-CDC device, or flow control auto-disabled) it falls
        back to the device-reported draw time (:attr:`last_timing`) so a fast
        sender can't overrun a slow draw.
        """
        if fps is not None and fps > 0:
            time.sleep(1.0 / fps)
            return
        if getattr(self.transport, "flow_control", None) is not None:
            return  # flow control already paces us at the device's real rate
        timing = self.last_timing
        if timing is not None and timing.draw_us > 0:
            time.sleep(timing.draw_us / 1_000_000)

    def send_keepalive(self) -> bytes:
        """Send a keepalive ping so an idle receiver holds the current frame.

        A frame-suppressing sender (see :attr:`suppress_duplicates`) goes silent
        when the scene is static; without traffic a v2 receiver eventually times
        out to its splash. A periodic keepalive proves the link is live without
        re-sending the whole frame. Honours flow control like a frame. Returns
        the bytes written.
        """
        data = protocol.keepalive()
        self.transport.write(data)
        self.transport.flush()
        return data

    # -- transmission ------------------------------------------------------

    def _transmit(self, data: bytes) -> bytes:
        """Write one frame's bytes, honouring duplicate suppression."""
        if self.suppress_duplicates and data == self._last_sent:
            self.frames_suppressed += 1
            return data
        self.transport.write(data)
        self.transport.flush()
        self._last_sent = data
        return data

    def send_frame(self) -> bytes:
        """Serialise and transmit the frame, then reset for the next one.

        Returns the exact bytes written (handy for tests and dry runs).
        """
        data = self._frame.to_bytes()
        self._transmit(data)
        self._frame.reset()
        return data

    def send_heightfield(
        self,
        cols: int,
        rows: int,
        x0: int,
        x_step: int,
        y0: int,
        y_step: int,
        y_scale: int,
        displacement: Any,
        brightness: int,
        *,
        intensity: Any | None = None,
        serpentine: bool = False,
    ) -> bytes:
        """Transmit a gridded scan as a ``HEIGHTFIELD`` frame (device coords).

        Sends the compact ``EXT`` command when the device advertised
        ``HEIGHTFIELD``; otherwise expands it to a base ``XY`` frame that draws
        identically on a v1 device. Returns the bytes transmitted.
        """
        if self.supports(Capability.HEIGHTFIELD):
            command = ext.encode_heightfield(
                cols,
                rows,
                x0,
                x_step,
                y0,
                y_step,
                y_scale,
                displacement,
                brightness,
                intensity=intensity,
                serpentine=serpentine,
            )
            data = ext.wrap_ext_frame(command, monochrome=self._frame.monochrome)
        else:
            segments = ext.expand_heightfield(
                cols,
                rows,
                x0,
                x_step,
                y0,
                y_step,
                y_scale,
                displacement,
                brightness,
                intensity=intensity,
                serpentine=serpentine,
            )
            data = ext.segments_to_base_frame(segments, monochrome=self._frame.monochrome)
        return self._transmit(data)

    def send_polyline(
        self,
        x0: int,
        y0: int,
        deltas: list[tuple[int, int]],
        brightness: int,
        *,
        intensity: list[int] | None = None,
        closed: bool = False,
        wide: bool = False,
    ) -> bytes:
        """Transmit a stroke as a ``POLYLINE`` frame (device coords).

        Sends the compact ``EXT`` command when the device advertised
        ``POLYLINE``; otherwise expands it to a base ``XY`` frame.
        """
        if self.supports(Capability.POLYLINE):
            command = ext.encode_polyline(
                x0, y0, deltas, brightness, intensity=intensity, closed=closed, wide=wide
            )
            data = ext.wrap_ext_frame(command, monochrome=self._frame.monochrome)
        else:
            segments = ext.expand_polyline(
                x0, y0, deltas, brightness, intensity=intensity, closed=closed, wide=wide
            )
            data = ext.segments_to_base_frame(segments, monochrome=self._frame.monochrome)
        return self._transmit(data)

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
