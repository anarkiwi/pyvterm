"""Wire-level encoding for the USB-DVG / pitrex *vecterm* serial protocol.

This module is **pure**: it has no I/O dependencies and can be imported and
unit-tested without a serial port or any hardware. It mirrors the command
encoding in `gtoal/pitrex`'s ``VMMenu/Win32/dvg/zvgFrame.c`` (the USB-DVG
drivers written by Mario Montminy, 2020) and cross-checks against the
canonical AdvanceMAME ``advance/osd/dvg.c`` implementation.

Every command is a single 32-bit word transmitted **big-endian** (most
significant byte first). The most significant three bits ``[31:29]`` select
the command::

    bit  31      29 28                                              0
         +---------+------------------------------------------------+
         |  flag   |  payload                                       |
         +---------+------------------------------------------------+

See ``docs/PROTOCOL.md`` for the full specification.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

__all__ = [
    "Flag",
    "Bounds",
    "DEFAULT_BOUNDS",
    "FLAG_SHIFT",
    "BLANK_SHIFT",
    "COORD_BITS",
    "COORD_MASK",
    "PAYLOAD_MASK",
    "DVG_RES_MIN",
    "DVG_RES_MAX",
    "DVG_RENDER_QUALITY",
    "COMPLETE_MONOCHROME",
    "pack_word",
    "scale_color",
    "encode_rgb_word",
    "encode_xy_word",
    "encode_frame_word",
    "encode_quality_word",
    "encode_complete_word",
    "encode_exit_word",
    "decode_word",
    "rgb",
    "rgb_scaled",
    "xy",
    "frame_header",
    "quality",
    "complete",
    "exit_command",
    # v2 extensions (docs/PROTOCOL-EXTENSIONS.md)
    "ExtSubtype",
    "Capability",
    "EXT_SUBTYPE_SHIFT",
    "EXT_LENGTH_MASK",
    "CMD_HELLO",
    "CMD_KEEPALIVE",
    "encode_keepalive_word",
    "keepalive",
    "HELLO_MAGIC",
    "HELLO_LENGTH",
    "PROTOCOL_VERSION",
    "encode_ext_header",
    "encode_hello_word",
    "hello_word",
    "HelloDescriptor",
    "decode_hello_descriptor",
]


class Flag(IntEnum):
    """Command selector occupying the top three bits of every word."""

    COMPLETE = 0x0  #: End-of-frame marker (payload 0, or the monochrome bit).
    RGB = 0x1  #: Set the colour/intensity of subsequent vectors.
    XY = 0x2  #: Move (beam off) or draw (beam on) to a coordinate.
    QUALITY = 0x3  #: Render-quality hint (pitrex/zvgFrame variant).
    FRAME = 0x4  #: Frame header carrying total beam-travel length.
    CMD = 0x5  #: Device command channel (AdvanceMAME GET_DVG_INFO; vekterm HELLO).
    EXT = 0x6  #: v2 extensions container (see ``docs/PROTOCOL-EXTENSIONS.md``).
    EXIT = 0x7  #: Tell the device the session is over.


# --- Bit layout -----------------------------------------------------------

FLAG_SHIFT = 29  #: The flag occupies bits [31:29].
BLANK_SHIFT = 28  #: The XY blank (beam-off) bit.
COORD_BITS = 14  #: Each XY coordinate is 14 bits wide.
COORD_MASK = (1 << COORD_BITS) - 1  #: 0x3FFF.
PAYLOAD_MASK = (1 << FLAG_SHIFT) - 1  #: 0x1FFFFFFF — bits available below the flag.

# --- Device resolution ----------------------------------------------------

DVG_RES_MIN = 0  #: Minimum DVG coordinate.
DVG_RES_MAX = 4095  #: Maximum DVG coordinate (12-bit DAC range).
DVG_RENDER_QUALITY = 5  #: Default quality value sent once per frame.

#: OR'd into a ``COMPLETE`` word to flag a black & white game (AdvanceMAME).
COMPLETE_MONOCHROME = 1 << 28

# --- v2 extensions (docs/PROTOCOL-EXTENSIONS.md) --------------------------

PROTOCOL_VERSION = 2  #: vekterm protocol version advertised in the HELLO reply.

EXT_SUBTYPE_SHIFT = 24  #: The EXT subtype occupies bits [28:24].
EXT_SUBTYPE_MASK = 0x1F  #: 5-bit subtype.
EXT_LENGTH_MASK = (1 << EXT_SUBTYPE_SHIFT) - 1  #: 0xFFFFFF — 24-bit payload length.


class ExtSubtype(IntEnum):
    """Subtype selector inside an ``EXT`` container word."""

    HEIGHTFIELD = 0x01  #: Gridded scan; X implicit, ~1 byte/point.
    POLYLINE = 0x02  #: Absolute anchor + signed deltas, ~2 bytes/point.
    HEIGHTFIELD_DELTA = 0x03  #: Reserved (temporal delta, not implemented).


class Capability(IntEnum):
    """Bits of the HELLO descriptor's capability bitmap."""

    HEIGHTFIELD = 0x01
    POLYLINE = 0x02
    INTENSITY = 0x04  #: Per-point intensity planes are honoured.
    FRAME_DELTA = 0x08  #: Reserved (temporal delta).


#: Subcommand byte of the ``CMD`` capability probe (``'V'``), distinct from
#: AdvanceMAME's ``GET_DVG_INFO = 1``.
CMD_HELLO = 0x56
#: Subcommand byte of the ``CMD`` keepalive / null ping (``'K'``). A sender writes
#: it to keep an idle receiver from timing out to its splash without re-sending a
#: whole frame (see ``docs/PROTOCOL-EXTENSIONS.md`` §11).
CMD_KEEPALIVE = 0x4B
#: First two bytes of the HELLO reply, identifying a vekterm device.
HELLO_MAGIC = b"VK"
#: Total length of the fixed binary HELLO descriptor.
HELLO_LENGTH = 12


@dataclass(frozen=True)
class Bounds:
    """The host coordinate space, mapped onto the device's ``0..4095`` grid.

    The defaults match ``zvgFrame.h`` (a 1024x768 space centred on the
    origin), which is the standard MAME vector resolution.
    """

    x_min: int = -512
    x_max: int = 511
    y_min: int = -384
    y_max: int = 383

    @property
    def width(self) -> int:
        return self.x_max - self.x_min

    @property
    def height(self) -> int:
        return self.y_max - self.y_min

    def conv_x(self, x: float) -> int:
        """Map a host X coordinate onto ``0..DVG_RES_MAX`` (``CONVX`` in C)."""
        return int(((x - self.x_min) * DVG_RES_MAX) // self.width)

    def conv_y(self, y: float) -> int:
        """Map a host Y coordinate onto ``0..DVG_RES_MAX`` (``CONVY`` in C)."""
        return int(((y - self.y_min) * DVG_RES_MAX) // self.height)


DEFAULT_BOUNDS = Bounds()


# --- Word encoders --------------------------------------------------------


def pack_word(word: int) -> bytes:
    """Serialise a 32-bit command ``word`` as 4 big-endian bytes."""
    return struct.pack(">I", word & 0xFFFFFFFF)


def scale_color(value: int) -> int:
    """Scale a ~4-bit colour channel to 8 bits, clamped to 255.

    Matches ``zvgFrameSetRGB15``: the input is shifted left by 4 (so ``15``
    maps to ``240``) and clamped, so values ``>= 16`` saturate at ``255``.
    """
    scaled = value << 4
    return 255 if scaled > 255 else scaled


def encode_rgb_word(r: int, g: int, b: int) -> int:
    """Encode a raw 8-bit-per-channel ``RGB`` word."""
    return (Flag.RGB << FLAG_SHIFT) | ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | (b & 0xFF)


def encode_xy_word(x: int, y: int, blank: bool) -> int:
    """Encode an ``XY`` word.

    ``blank`` selects beam-off (a move) when ``True`` and beam-on (a draw)
    when ``False``.
    """
    return (
        (Flag.XY << FLAG_SHIFT)
        | ((1 if blank else 0) << BLANK_SHIFT)
        | ((x & COORD_MASK) << COORD_BITS)
        | (y & COORD_MASK)
    )


def encode_frame_word(vector_length: int) -> int:
    """Encode the ``FRAME`` header carrying total beam-travel length."""
    return (Flag.FRAME << FLAG_SHIFT) | (vector_length & PAYLOAD_MASK)


def encode_quality_word(value: int = DVG_RENDER_QUALITY) -> int:
    """Encode the ``QUALITY`` render hint."""
    return (Flag.QUALITY << FLAG_SHIFT) | (value & PAYLOAD_MASK)


def encode_complete_word(monochrome: bool = False) -> int:
    """Encode the ``COMPLETE`` end-of-frame marker."""
    return (Flag.COMPLETE << FLAG_SHIFT) | (COMPLETE_MONOCHROME if monochrome else 0)


def encode_exit_word() -> int:
    """Encode the ``EXIT`` (session over) command."""
    return Flag.EXIT << FLAG_SHIFT


def decode_word(word: int) -> dict[str, Any]:
    """Decode a 32-bit word into a human-readable ``dict`` (for tests/debug)."""
    flag = Flag((word >> FLAG_SHIFT) & 0x7)
    info: dict[str, Any] = {"flag": flag}
    if flag is Flag.RGB:
        info.update(r=(word >> 16) & 0xFF, g=(word >> 8) & 0xFF, b=word & 0xFF)
    elif flag is Flag.XY:
        info.update(
            blank=bool((word >> BLANK_SHIFT) & 0x1),
            x=(word >> COORD_BITS) & COORD_MASK,
            y=word & COORD_MASK,
        )
    elif flag is Flag.FRAME:
        info["vector_length"] = word & PAYLOAD_MASK
    elif flag is Flag.QUALITY:
        info["value"] = word & PAYLOAD_MASK
    elif flag is Flag.COMPLETE:
        info["monochrome"] = bool(word & COMPLETE_MONOCHROME)
    return info


# --- v2 extensions: EXT container + HELLO negotiation ---------------------


def encode_ext_header(subtype: int, length: int) -> bytes:
    """4 big-endian bytes for an ``EXT`` container header.

    ``subtype`` selects the payload format (:class:`ExtSubtype`); ``length`` is
    the number of byte-packed payload bytes that follow the header.
    """
    word = (
        (Flag.EXT << FLAG_SHIFT)
        | ((subtype & EXT_SUBTYPE_MASK) << EXT_SUBTYPE_SHIFT)
        | (length & EXT_LENGTH_MASK)
    )
    return pack_word(word)


def encode_hello_word() -> int:
    """Encode the ``CMD`` capability-probe word a sender writes to detect vekterm."""
    return (Flag.CMD << FLAG_SHIFT) | CMD_HELLO


def hello_word() -> bytes:
    """4 bytes for the capability probe (``encode_hello_word`` packed)."""
    return pack_word(encode_hello_word())


def encode_keepalive_word() -> int:
    """Encode the ``CMD`` keepalive / null ping word."""
    return (Flag.CMD << FLAG_SHIFT) | CMD_KEEPALIVE


def keepalive() -> bytes:
    """4 bytes for the keepalive ping (``encode_keepalive_word`` packed)."""
    return pack_word(encode_keepalive_word())


@dataclass(frozen=True)
class HelloDescriptor:
    """A decoded vekterm ``HELLO`` reply (capability descriptor)."""

    version: int
    capabilities: int
    coord_bits: int
    brightness_bits: int
    max_pipeline: int
    max_payload: int
    refresh_hz: int

    def supports(self, capability: Capability) -> bool:
        """True if ``capability`` is advertised in the bitmap."""
        return bool(self.capabilities & capability)


def decode_hello_descriptor(data: bytes) -> HelloDescriptor | None:
    """Decode a 12-byte ``HELLO`` descriptor, or ``None`` if it is malformed.

    ``data`` may contain leading bytes (e.g. flow-control ``0x06``); the magic
    ``VK`` is located first, so a caller can hand over whatever it read.
    """
    idx = data.find(HELLO_MAGIC)
    if idx < 0 or len(data) - idx < HELLO_LENGTH:
        return None
    d = data[idx : idx + HELLO_LENGTH]
    return HelloDescriptor(
        version=d[2],
        capabilities=d[3],
        coord_bits=d[4],
        brightness_bits=d[5],
        max_pipeline=(d[6] << 8) | d[7],
        max_payload=(d[8] << 8) | d[9],
        refresh_hz=d[10],
    )


# --- Byte-producing convenience wrappers ----------------------------------


def rgb(r: int, g: int, b: int) -> bytes:
    """4 bytes setting a raw 8-bit-per-channel colour."""
    return pack_word(encode_rgb_word(r, g, b))


def rgb_scaled(r: int, g: int, b: int) -> bytes:
    """4 bytes setting a colour from ~4-bit channels (``zvgFrameSetRGB15``)."""
    return pack_word(encode_rgb_word(scale_color(r), scale_color(g), scale_color(b)))


def xy(x: int, y: int, blank: bool) -> bytes:
    """4 bytes moving (``blank=True``) or drawing (``blank=False``) to ``x,y``."""
    return pack_word(encode_xy_word(x, y, blank))


def frame_header(vector_length: int) -> bytes:
    """4 bytes for the frame header."""
    return pack_word(encode_frame_word(vector_length))


def quality(value: int = DVG_RENDER_QUALITY) -> bytes:
    """4 bytes for the quality hint."""
    return pack_word(encode_quality_word(value))


def complete(monochrome: bool = False) -> bytes:
    """4 bytes for the end-of-frame marker."""
    return pack_word(encode_complete_word(monochrome))


def exit_command() -> bytes:
    """4 bytes telling the device the session is over."""
    return pack_word(encode_exit_word())
