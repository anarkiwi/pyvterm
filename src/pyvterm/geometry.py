"""Geometry helpers used by the frame builder.

Pure functions only — no hardware or I/O. ``clip_line`` is a direct port of
the Cohen-Sutherland clipper in ``zvgFrame.c`` (some games emit coordinates
outside the view window, so vectors must be clipped before transmission).
"""

from __future__ import annotations

import math

__all__ = ["INSIDE", "LEFT", "RIGHT", "BOTTOM", "TOP", "clip_line", "vector_length"]

# Region codes (matching zvgFrame.c).
INSIDE = 0
LEFT = 1
RIGHT = 2
BOTTOM = 4
TOP = 8

Window = tuple[float, float, float, float]  # (x_min, y_min, x_max, y_max)
Point = tuple[float, float]
Segment = tuple[float, float, float, float]


def _region_code(x: float, y: float, window: Window) -> int:
    x_min, y_min, x_max, y_max = window
    code = INSIDE
    if x < x_min:
        code |= LEFT
    elif x > x_max:
        code |= RIGHT
    if y < y_min:
        code |= BOTTOM
    elif y > y_max:
        code |= TOP
    return code


def clip_line(x1: float, y1: float, x2: float, y2: float, window: Window) -> Segment | None:
    """Cohen-Sutherland line clip.

    Returns the clipped ``(x1, y1, x2, y2)`` segment, or ``None`` if the line
    lies entirely outside ``window`` (``(x_min, y_min, x_max, y_max)``).
    """
    x_min, y_min, x_max, y_max = window
    code1 = _region_code(x1, y1, window)
    code2 = _region_code(x2, y2, window)

    while True:
        if code1 == 0 and code2 == 0:
            return (x1, y1, x2, y2)
        if code1 & code2:
            return None

        code_out = code1 or code2
        x = y = 0.0
        if code_out & TOP:
            x = x1 + (x2 - x1) * (y_max - y1) / (y2 - y1)
            y = y_max
        elif code_out & BOTTOM:
            x = x1 + (x2 - x1) * (y_min - y1) / (y2 - y1)
            y = y_min
        elif code_out & RIGHT:
            y = y1 + (y2 - y1) * (x_max - x1) / (x2 - x1)
            x = x_max
        elif code_out & LEFT:
            y = y1 + (y2 - y1) * (x_min - x1) / (x2 - x1)
            x = x_min

        if code_out == code1:
            x1, y1 = x, y
            code1 = _region_code(x1, y1, window)
        else:
            x2, y2 = x, y
            code2 = _region_code(x2, y2, window)


def vector_length(x0: float, y0: float, x1: float, y1: float) -> int:
    """Integer Euclidean length of a segment (``vector_length`` in C)."""
    return int(math.hypot(x1 - x0, y1 - y0))
