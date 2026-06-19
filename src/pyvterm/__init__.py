"""pyvterm — drive a pitrex/Vectrex over a serial port from Python.

pyvterm speaks the **USB-DVG / *vecterm* serial protocol** used by the
`gtoal/pitrex <https://github.com/gtoal/pitrex>`_ project: the same wire
format a custom MAME build uses to push vector frames to a Vectrex. With it
you can act as the "custom MAME" and draw vectors on real hardware from
Python.

Quick start
-----------
>>> from pyvterm import VectorTerminal
>>> with VectorTerminal(port="/dev/ttyACM0") as vt:   # doctest: +SKIP
...     with vt.frame():
...         vt.set_intensity(15)
...         vt.polyline([(0, 0), (100, 100), (-100, 100)], closed=True)

For tests and dry runs, pass a :class:`MemoryTransport` instead of a port.
"""

from __future__ import annotations

from . import geometry, protocol
from .frame import FrameBuilder
from .geometry import clip_line, vector_length
from .protocol import (
    DEFAULT_BOUNDS,
    DVG_RENDER_QUALITY,
    DVG_RES_MAX,
    DVG_RES_MIN,
    Bounds,
    Flag,
)
from .terminal import VectorTerminal
from .transport import (
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    DEFAULT_SYNC_BYTE,
    MemoryTransport,
    SerialTransport,
    Transport,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # high level
    "VectorTerminal",
    "FrameBuilder",
    # transports
    "Transport",
    "SerialTransport",
    "MemoryTransport",
    "DEFAULT_PORT",
    "DEFAULT_BAUDRATE",
    "DEFAULT_SYNC_BYTE",
    # protocol + geometry
    "protocol",
    "geometry",
    "Flag",
    "Bounds",
    "DEFAULT_BOUNDS",
    "DVG_RES_MIN",
    "DVG_RES_MAX",
    "DVG_RENDER_QUALITY",
    "clip_line",
    "vector_length",
]
