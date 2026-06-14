"""Stateful frame assembly.

:class:`FrameBuilder` accumulates colour and vector commands and serialises a
complete frame to bytes. It is **transport-agnostic and pure** — it never
touches a serial port — so the exact wire output can be asserted in tests.

The emitted frame layout matches ``zvgFrame.c``'s ``serial_send``::

    [FRAME | total_vector_length]   <- header, written first
    [RGB ...] [XY ...] [XY ...] ... <- body (colours and vectors)
    [QUALITY | value]
    [COMPLETE]
"""

from __future__ import annotations

from . import protocol
from .geometry import clip_line, vector_length
from .protocol import DEFAULT_BOUNDS, DVG_RENDER_QUALITY, DVG_RES_MAX, DVG_RES_MIN, Bounds

__all__ = ["FrameBuilder"]

Window = tuple[int, int, int, int]


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


class FrameBuilder:
    """Accumulate a single frame of vectors and serialise it to bytes.

    Parameters
    ----------
    bounds:
        Host coordinate space mapped onto the device grid.
    clip_window:
        ``(x_min, y_min, x_max, y_max)`` clip rectangle in host coordinates.
        Defaults to the full ``bounds`` (the C code requires the caller to set
        this; we default it so drawing works out of the box).
    quality:
        Value sent in the per-frame ``QUALITY`` command.
    monochrome:
        Set the monochrome bit in the ``COMPLETE`` marker.
    """

    def __init__(
        self,
        bounds: Bounds = DEFAULT_BOUNDS,
        clip_window: Window | None = None,
        quality: int = DVG_RENDER_QUALITY,
        monochrome: bool = False,
    ) -> None:
        self.bounds = bounds
        self.quality = quality
        self.monochrome = monochrome
        if clip_window is None:
            clip_window = (bounds.x_min, bounds.y_min, bounds.x_max, bounds.y_max)
        self.clip_window: Window = clip_window
        self.reset()

    def reset(self) -> None:
        """Discard all accumulated commands and start a fresh frame."""
        self._body = bytearray()
        self._vector_length = 0
        self._last: tuple[int, int] | None = None  # last device-space endpoint
        self._last_black = True  # colour starts at (0, 0, 0)
        self._count = 0

    # -- properties --------------------------------------------------------

    @property
    def vector_count(self) -> int:
        """Number of vectors accepted into the current frame."""
        return self._count

    @property
    def total_length(self) -> int:
        """Accumulated beam-travel length (sent in the FRAME header)."""
        return self._vector_length

    def __len__(self) -> int:
        return self._count

    # -- drawing -----------------------------------------------------------

    def set_clip_window(self, x_min: int, y_min: int, x_max: int, y_max: int) -> None:
        """Set the clip rectangle in host coordinates (``zvgFrameSetClipWin``)."""
        self.clip_window = (x_min, y_min, x_max, y_max)

    def set_rgb(self, r: int, g: int, b: int) -> None:
        """Set the colour of subsequent vectors from ~4-bit channels.

        Mirrors ``zvgFrameSetRGB15``: each channel is scaled ``<< 4`` and
        clamped to 255. A colour of ``(0, 0, 0)`` blanks subsequent draws.
        """
        r8 = protocol.scale_color(r)
        g8 = protocol.scale_color(g)
        b8 = protocol.scale_color(b)
        self._last_black = r8 == 0 and g8 == 0 and b8 == 0
        self._body += protocol.rgb(r8, g8, b8)

    def vector(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        """Add a vector from ``(x0, y0)`` to ``(x1, y1)`` in host coordinates.

        The line is clipped to the clip window; returns ``False`` (emitting
        nothing) when it lies entirely off-screen. A beam-off reposition is
        inserted automatically when the start does not continue the previous
        vector, so connected polylines cost only one extra move overall.
        """
        clipped = clip_line(x0, y0, x1, y1, self.clip_window)
        if clipped is None:
            return False
        cx0, cy0, cx1, cy1 = clipped

        start = (
            _clamp(self.bounds.conv_x(cx0), DVG_RES_MIN, DVG_RES_MAX),
            _clamp(self.bounds.conv_y(cy0), DVG_RES_MIN, DVG_RES_MAX),
        )
        end = (
            _clamp(self.bounds.conv_x(cx1), DVG_RES_MIN, DVG_RES_MAX),
            _clamp(self.bounds.conv_y(cy1), DVG_RES_MIN, DVG_RES_MAX),
        )

        if self._last is not None:
            self._vector_length += vector_length(self._last[0], self._last[1], *start)
        self._vector_length += vector_length(start[0], start[1], end[0], end[1])

        if self._last != start:
            # Reposition the beam (always off) to the start of this vector.
            self._body += protocol.xy(start[0], start[1], blank=True)
        self._body += protocol.xy(end[0], end[1], blank=self._last_black)

        self._last = end
        self._count += 1
        return True

    def polyline(self, points: list[tuple[float, float]], closed: bool = False) -> int:
        """Draw a connected sequence of points; returns the vectors emitted."""
        emitted = 0
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if self.vector(x0, y0, x1, y1):
                emitted += 1
        if closed and len(points) > 2:
            x0, y0 = points[-1]
            x1, y1 = points[0]
            if self.vector(x0, y0, x1, y1):
                emitted += 1
        return emitted

    # -- serialisation -----------------------------------------------------

    def to_bytes(self) -> bytes:
        """Serialise the accumulated frame (header + body + quality + complete)."""
        out = bytearray()
        out += protocol.frame_header(self._vector_length)
        out += self._body
        out += protocol.quality(self.quality)
        out += protocol.complete(self.monochrome)
        return bytes(out)
