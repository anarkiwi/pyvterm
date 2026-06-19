"""Byte transports for delivering frames to the device.

:class:`SerialTransport` talks to a real USB-DVG / pitrex over a serial port
(via `pyserial`). :class:`MemoryTransport` records bytes in memory for tests
and dry runs. Both implement the small :class:`Transport` interface, so the
rest of the library never needs to know which is in use.
"""

from __future__ import annotations

import abc
import time
from typing import Any, Callable

__all__ = [
    "Transport",
    "MemoryTransport",
    "SerialTransport",
    "DEFAULT_BAUDRATE",
    "DEFAULT_PORT",
    "DEFAULT_SYNC_BYTE",
]

#: USB-CDC devices ignore the line rate, but the reference driver requests 2 Mbaud.
DEFAULT_BAUDRATE = 2_000_000
#: Default device path created by the USB-DVG / pitrex CDC gadget on Linux.
DEFAULT_PORT = "/dev/ttyACM0"
#: Byte a flow-controlled receiver (e.g. vekterm on a raw UART) sends to say
#: "ready for the next frame". USB-CDC devices don't need this; raw-UART ones do.
DEFAULT_SYNC_BYTE = 0x06  # ASCII ACK


class Transport(abc.ABC):
    """Minimal sink for outgoing protocol bytes."""

    @abc.abstractmethod
    def write(self, data: bytes) -> int:
        """Write ``data`` and return the number of bytes written."""

    def read(self, size: int = 1) -> bytes:
        """Read up to ``size`` bytes (default: not supported, returns empty)."""
        return b""

    def flush(self) -> None:  # noqa: B027 - optional no-op hook, not abstract
        """Block until buffered output has been transmitted."""

    def close(self) -> None:  # noqa: B027 - optional no-op hook, not abstract
        """Release any underlying resources."""

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class MemoryTransport(Transport):
    """In-memory transport that records every byte written.

    Useful for unit tests and for running examples without hardware.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.frames: list[bytes] = []
        self.closed = False
        self.flushed = 0

    def write(self, data: bytes) -> int:
        self.buffer += data
        self.frames.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        self.flushed += 1

    def close(self) -> None:
        self.closed = True

    def getvalue(self) -> bytes:
        """Return everything written so far."""
        return bytes(self.buffer)


class SerialTransport(Transport):
    """Serial-port transport backed by `pyserial`.

    Parameters
    ----------
    port:
        Serial device path (e.g. ``/dev/ttyACM0`` or ``COM3``).
    baudrate:
        Nominal line rate; ignored by USB-CDC devices but set to match the
        reference driver.
    settle:
        Seconds to wait after opening before flushing buffers. The reference
        driver sleeps 2 s "to make flush work, for some reason"; keep it for
        real hardware, set ``0`` in tests.
    chunk_size:
        Writes are split into chunks of this many bytes (the reference driver
        uses 1024).
    serial_factory:
        Advanced/testing hook: a callable used in place of ``serial.Serial``.
    """

    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baudrate: int = DEFAULT_BAUDRATE,
        *,
        timeout: float | None = 1.0,
        write_timeout: float | None = None,
        settle: float = 2.0,
        chunk_size: int = 1024,
        flow_control: int | None = DEFAULT_SYNC_BYTE,
        sync_timeout: float = 1.0,
        serial_factory: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if serial_factory is None:
            try:
                import serial
            except ImportError as exc:  # pragma: no cover - exercised via factory
                raise ImportError(
                    "pyserial is required for SerialTransport; "
                    "install it with `pip install pyvterm` or `pip install pyserial`."
                ) from exc
            serial_factory = serial.Serial

        self.port = port
        self.baudrate = baudrate
        self.chunk_size = max(1, chunk_size)
        #: When set (a byte value), wait for the receiver to send it before each
        #: frame — flow control for a raw-UART receiver with no buffering (e.g.
        #: vekterm). On by default; if the receiver never sends the byte (a
        #: USB-CDC device that doesn't speak the handshake), it auto-disables
        #: after the first timeout and streams. Pass ``flow_control=None`` to
        #: force plain streaming from the start.
        self.flow_control = None if flow_control is None else (flow_control & 0xFF)
        self.sync_timeout = sync_timeout
        self._flow_seen = False  # have we ever received the ready byte?
        #: Diagnostics: frames actually transmitted vs skipped (no ready byte).
        self.frames_sent = 0
        self.frames_skipped = 0
        # 8N1, no flow control — matches the reference termios/DCB setup.
        self._serial = serial_factory(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            rtscts=False,
            xonxoff=False,
            dsrdtr=False,
            timeout=timeout,
            write_timeout=write_timeout,
            **kwargs,
        )
        if settle:
            time.sleep(settle)
        # Match `tcflush(..., TCIOFLUSH)` after the settle delay.
        for method in ("reset_input_buffer", "reset_output_buffer"):
            fn = getattr(self._serial, method, None)
            if callable(fn):
                fn()

    def _wait_ready(self) -> bool:
        """Block until the receiver sends the flow-control sync byte.

        Returns ``True`` once seen. On timeout: if we've *never* seen the byte the
        receiver doesn't speak the handshake (e.g. a USB-CDC device), so flow
        control auto-disables and we stream from now on; otherwise the handshake
        is in use and we return ``False`` so the caller skips the frame rather
        than overrun the receiver.
        """
        if self.flow_control is None:
            return True
        deadline = time.monotonic() + self.sync_timeout
        while time.monotonic() < deadline:
            waiting = getattr(self._serial, "in_waiting", 0) or 1
            chunk = self._serial.read(waiting)
            if chunk and self.flow_control in chunk:
                self._flow_seen = True
                return True
        if not self._flow_seen:
            self.flow_control = None  # no handshake here — stream (USB-CDC)
            return True
        return False

    def write(self, data: bytes) -> int:
        # With flow control, send a frame only after the receiver says it's ready;
        # if it never does, skip this frame (lossless beats overrunning the FIFO).
        if self.flow_control is not None and not self._wait_ready():
            self.frames_skipped += 1
            return 0
        view = memoryview(data)
        total = 0
        for offset in range(0, len(view), self.chunk_size):
            written = self._serial.write(view[offset : offset + self.chunk_size])
            total += int(written or 0)
        self.frames_sent += 1
        return total

    def read(self, size: int = 1) -> bytes:
        return bytes(self._serial.read(size))

    def flush(self) -> None:
        self._serial.flush()

    def close(self) -> None:
        self._serial.close()
