"""Tests for the byte transports (no real hardware required)."""

from typing import Any

from pyvterm.transport import MemoryTransport, SerialTransport


def test_memory_transport_records_writes():
    mt = MemoryTransport()
    assert mt.write(b"abc") == 3
    assert mt.write(b"de") == 2
    assert mt.getvalue() == b"abcde"
    assert mt.frames == [b"abc", b"de"]
    mt.flush()
    assert mt.flushed == 1
    mt.close()
    assert mt.closed is True


def test_memory_transport_context_manager_closes():
    with MemoryTransport() as mt:
        mt.write(b"x")
    assert mt.closed is True


class FakeSerial:
    """Stand-in for ``serial.Serial`` that records what the transport does."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.writes: list[bytes] = []
        self.flushed = 0
        self.closed = False
        self.reset_in = 0
        self.reset_out = 0

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        self.writes.append(chunk)
        return len(chunk)

    def read(self, size: int) -> bytes:
        return b"\x00" * size

    def flush(self) -> None:
        self.flushed += 1

    def close(self) -> None:
        self.closed = True

    def reset_input_buffer(self) -> None:
        self.reset_in += 1

    def reset_output_buffer(self) -> None:
        self.reset_out += 1


def _make(**kw: Any) -> tuple[SerialTransport, FakeSerial]:
    captured: dict[str, FakeSerial] = {}

    def factory(**kwargs: Any) -> FakeSerial:
        fake = FakeSerial(**kwargs)
        captured["fake"] = fake
        return fake

    transport = SerialTransport("/dev/ttyTEST", settle=0, serial_factory=factory, **kw)
    return transport, captured["fake"]


def test_serial_transport_opens_8n1_no_flow_control():
    transport, fake = _make()
    assert fake.kwargs["port"] == "/dev/ttyTEST"
    assert fake.kwargs["baudrate"] == 2_000_000
    assert fake.kwargs["bytesize"] == 8
    assert fake.kwargs["parity"] == "N"
    assert fake.kwargs["stopbits"] == 1
    assert fake.kwargs["rtscts"] is False
    assert fake.kwargs["xonxoff"] is False
    assert fake.kwargs["dsrdtr"] is False
    # Buffers are flushed on open even when settle is skipped.
    assert fake.reset_in == 1
    assert fake.reset_out == 1
    assert transport.port == "/dev/ttyTEST"


def test_serial_transport_chunks_large_writes():
    transport, fake = _make()
    assert transport.write(b"x" * 3000) == 3000
    assert [len(w) for w in fake.writes] == [1024, 1024, 952]


def test_serial_transport_custom_chunk_size():
    transport, fake = _make(chunk_size=4)
    assert transport.write(b"abcdefg") == 7
    assert [len(w) for w in fake.writes] == [4, 3]


def test_serial_transport_read_flush_close_delegate():
    transport, fake = _make()
    assert transport.read(4) == b"\x00\x00\x00\x00"
    transport.flush()
    assert fake.flushed == 1
    transport.close()
    assert fake.closed is True
