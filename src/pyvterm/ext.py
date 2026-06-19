"""v2 extension encoders for the USB-DVG / *vecterm* protocol.

These build the byte-packed ``EXT`` payloads specified in
``docs/PROTOCOL-EXTENSIONS.md`` and used only after a device advertises support
(:func:`pyvterm.protocol.decode_hello_descriptor`). Like :mod:`pyvterm.protocol`
this module is **pure** — no I/O — and works entirely in **device coordinates**
(``0..4095``); map from host space with :class:`pyvterm.protocol.Bounds` first.

Every subtype has:

* an ``encode_*`` function producing the full ``EXT`` command bytes, and
* an ``expand_*`` function producing the canonical list of device-space
  :data:`Segment`\\ s the receiver reconstructs — the contract both ends agree
  on, and the input to :func:`segments_to_base_frame`, the software fallback
  that renders effect-identically on an unmodified (v1) device.
"""

from __future__ import annotations

import math
import struct
from typing import Any

from . import protocol
from .protocol import (
    DVG_RENDER_QUALITY,
    DVG_RES_MAX,
    DVG_RES_MIN,
    ExtSubtype,
    encode_ext_header,
)

__all__ = [
    "Segment",
    "encode_heightfield",
    "expand_heightfield",
    "encode_polyline",
    "expand_polyline",
    "segments_to_base_frame",
    "wrap_ext_frame",
]

#: One reconstructed vector: ``(x0, y0, x1, y1, brightness)`` in device units.
Segment = tuple[int, int, int, int, int]

# Header struct layouts (big-endian, matching the command words).
_HEIGHTFIELD_HDR = struct.Struct(">BHHhhhhHB")  # flags,cols,rows,x0,xs,y0,ys,yscale,bright
_POLYLINE_HDR = struct.Struct(">BBHHH")  # flags,bright,x0,y0,count

# HEIGHTFIELD flag bits.
_HF_INTENSITY = 0x01
_HF_SERPENTINE = 0x02
# POLYLINE flag bits.
_PL_INTENSITY = 0x01
_PL_CLOSED = 0x02
_PL_WIDE = 0x04


def _clamp_dev(value: int) -> int:
    return DVG_RES_MIN if value < DVG_RES_MIN else DVG_RES_MAX if value > DVG_RES_MAX else value


def _as_bytes(values: Any, length: int, name: str) -> bytes:
    """Coerce a byte buffer / int sequence / numpy uint8 array to ``bytes``."""
    data = bytes(values)  # works for bytes, bytearray, list[int], and uint8 ndarrays
    if len(data) != length:
        raise ValueError(f"{name} must have {length} bytes, got {len(data)}")
    return data


# --- HEIGHTFIELD (subtype 0x01) -------------------------------------------


def encode_heightfield(
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
    """Encode an ``EXT`` ``HEIGHTFIELD`` command (gridded scan, ~1 byte/point).

    ``displacement`` is ``rows*cols`` bytes (``0..255``) of relief; the optional
    ``intensity`` plane is the same length and unlocks per-point brightness and
    free dark-gap blanking (intensity ``0`` breaks the scan-line run).
    """
    n = rows * cols
    disp = _as_bytes(displacement, n, "displacement")
    inten = _as_bytes(intensity, n, "intensity") if intensity is not None else None
    flags = (_HF_INTENSITY if inten is not None else 0) | (_HF_SERPENTINE if serpentine else 0)
    header = _HEIGHTFIELD_HDR.pack(
        flags, cols, rows, x0, x_step, y0, y_step, y_scale, brightness & 0xFF
    )
    payload = header + disp + (inten or b"")
    return encode_ext_header(ExtSubtype.HEIGHTFIELD, len(payload)) + payload


def expand_heightfield(
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
) -> list[Segment]:
    """Reconstruct the device-space segments a ``HEIGHTFIELD`` command produces."""
    n = rows * cols
    disp = _as_bytes(displacement, n, "displacement")
    inten = _as_bytes(intensity, n, "intensity") if intensity is not None else None
    segments: list[Segment] = []
    for r in range(rows):
        y_base = y0 + r * y_step
        columns = range(cols - 1, -1, -1) if (serpentine and r & 1) else range(cols)
        have = False
        px = py = 0
        for c in columns:
            idx = r * cols + c
            z = inten[idx] if inten is not None else (brightness & 0xFF)
            if z == 0:
                have = False  # blanked: break the run (dark gap)
                continue
            x = _clamp_dev(x0 + c * x_step)
            y = _clamp_dev(y_base + ((disp[idx] * y_scale) >> 8))
            if have:
                segments.append((px, py, x, y, z))
            px, py, have = x, y, True
    return segments


# --- POLYLINE (subtype 0x02) ----------------------------------------------


def encode_polyline(
    x0: int,
    y0: int,
    deltas: list[tuple[int, int]],
    brightness: int,
    *,
    intensity: list[int] | None = None,
    closed: bool = False,
    wide: bool = False,
) -> bytes:
    """Encode an ``EXT`` ``POLYLINE`` command (absolute anchor + signed deltas).

    ``deltas`` are ``count-1`` ``(dx, dy)`` steps from the previous point; they
    are 8-bit unless ``wide`` (16-bit). ``intensity`` (one per delta) unlocks
    per-point brightness.
    """
    if intensity is not None and len(intensity) != len(deltas):
        raise ValueError("intensity must have one entry per delta")
    flags = (
        (_PL_INTENSITY if intensity is not None else 0)
        | (_PL_CLOSED if closed else 0)
        | (_PL_WIDE if wide else 0)
    )
    header = _POLYLINE_HDR.pack(flags, brightness & 0xFF, x0, y0, len(deltas) + 1)
    step = struct.Struct(">hh") if wide else struct.Struct(">bb")
    body = bytearray()
    for i, (dx, dy) in enumerate(deltas):
        body += step.pack(dx, dy)
        if intensity is not None:
            body.append(intensity[i] & 0xFF)
    payload = header + bytes(body)
    return encode_ext_header(ExtSubtype.POLYLINE, len(payload)) + payload


def expand_polyline(
    x0: int,
    y0: int,
    deltas: list[tuple[int, int]],
    brightness: int,
    *,
    intensity: list[int] | None = None,
    closed: bool = False,
    wide: bool = False,
) -> list[Segment]:
    """Reconstruct the device-space segments a ``POLYLINE`` command produces."""
    del wide  # geometry is identical; the flag only widens the wire deltas
    segments: list[Segment] = []
    x, y = x0, y0
    px, py = _clamp_dev(x0), _clamp_dev(y0)
    for i, (dx, dy) in enumerate(deltas):
        x += dx
        y += dy
        z = intensity[i] if intensity is not None else (brightness & 0xFF)
        cx, cy = _clamp_dev(x), _clamp_dev(y)
        if z > 0:
            segments.append((px, py, cx, cy, z))
        px, py = cx, cy
    if closed and deltas and (brightness & 0xFF) > 0:
        cx, cy = _clamp_dev(x0), _clamp_dev(y0)
        segments.append((px, py, cx, cy, brightness & 0xFF))
    return segments


# --- software fallback: device-space segments -> a base (v1) frame --------


def segments_to_base_frame(
    segments: list[Segment],
    *,
    quality: int = DVG_RENDER_QUALITY,
    monochrome: bool = False,
) -> bytes:
    """Serialise device-space ``segments`` as a base ``FRAME``/``XY`` frame.

    The output draws identically on an unmodified (v1) vecterm, so an extension
    encoder can be validated against :doc:`PROTOCOL.md` and used as a fall-back
    when the device doesn't advertise the subtype.
    """
    body = bytearray()
    last: tuple[int, int] | None = None
    last_b: int | None = None
    total = 0
    for x0, y0, x1, y1, b in segments:
        if last is not None:
            total += int(math.hypot(x0 - last[0], y0 - last[1]))
        total += int(math.hypot(x1 - x0, y1 - y0))
        if b != last_b:
            body += protocol.rgb(b, b, b)
            last_b = b
        if last != (x0, y0):
            body += protocol.xy(x0, y0, blank=True)
        body += protocol.xy(x1, y1, blank=(b == 0))
        last = (x1, y1)
    out = bytearray()
    out += protocol.frame_header(total)
    out += body
    out += protocol.quality(quality)
    out += protocol.complete(monochrome)
    return bytes(out)


def wrap_ext_frame(ext_bytes: bytes, *, monochrome: bool = False) -> bytes:
    """Wrap one ``EXT`` command in the ``FRAME``/``COMPLETE`` frame envelope.

    ``FRAME`` resets the receiver's frame and ``COMPLETE`` publishes it (driving
    the per-frame flow-control handshake), with the ``EXT`` payload in between
    expanding into the frame's vectors.
    """
    return protocol.frame_header(0) + ext_bytes + protocol.complete(monochrome)
