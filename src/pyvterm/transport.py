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

__all__ = ["Transport", "MemoryTransport", "SerialTransport", "DEFAULT_BAUDRATE", "DEFAULT_PORT"]

#: USB-CDC devices ignore the line rate, but the reference driver requests 2 Mbaud.
DEFAULT_BAUDRATE = 2_000_000
#: Default device path created by the USB-DVG / pitrex CDC gadget on Linux.
DEFAULT_PORT = "/dev/ttyACM0"


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

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        total = 0
        for offset in range(0, len(view), self.chunk_size):
            written = self._serial.write(view[offset : offset + self.chunk_size])
            total += int(written or 0)
        return total

    def read(self, size: int = 1) -> bytes:
        return bytes(self._serial.read(size))

    def flush(self) -> None:
        self._serial.flush()

    def close(self) -> None:
        self._serial.close()
