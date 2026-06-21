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

    # These tests exercise raw writes; flow control is on by default but is
    # covered by its own tests, so default it off here unless asked for.
    kw.setdefault("flow_control", None)
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


def test_serial_transport_flow_control_sends_after_ready():
    transport, fake = _make(flow_control=0x06, sync_timeout=0.5)
    fake.read = lambda n=1: b"\x06"  # receiver says "ready"  # type: ignore[assignment]
    assert transport.write(b"\x01\x02\x03\x04") == 4
    assert transport.frames_sent == 1
    assert transport.flow_control == 0x06  # stays on once the byte is seen


def test_serial_transport_v2_decodes_timing_record():
    # Once v2 is negotiated, the receiver's reply IS the 5-byte timing record
    # (draw_us=0x04D2=1234, vectors=0x0064=100, flags=0) — no sync byte, no
    # marker. Its arrival is the readiness signal, so the frame sends.
    transport, fake = _make(flow_control=0x06, sync_timeout=0.5)
    transport._v2 = True
    fake.read = lambda n=1: b"\x04\xd2\x00\x64\x00"  # type: ignore[assignment]
    assert transport.write(b"\x01\x02\x03\x04") == 4
    assert transport.frames_sent == 1
    timing = transport.last_timing
    assert timing is not None
    assert timing.draw_us == 1234
    assert timing.vectors == 100
    assert timing.overflow is False
    assert timing.idle is False
    assert abs(timing.max_fps - 1_000_000 / 1234) < 1e-6


def test_serial_transport_v2_timing_flags_overflow_and_idle():
    transport, fake = _make(flow_control=0x06, sync_timeout=0.5)
    transport._v2 = True
    # flags = overflow|idle (0x03), draw_us=0, vectors=0.
    fake.read = lambda n=1: b"\x00\x00\x00\x00\x03"  # type: ignore[assignment]
    assert transport.write(b"\x01\x02\x03\x04") == 4
    timing = transport.last_timing
    assert timing is not None
    assert timing.overflow is True
    assert timing.idle is True
    assert timing.max_fps == float("inf")


def test_serial_transport_v2_record_split_across_reads():
    # The record arrives in fragments; the transport reassembles it and only
    # releases the frame once the whole 5-byte record has been read.
    transport, fake = _make(flow_control=0x06, sync_timeout=0.5)
    transport._v2 = True
    chunks = iter([b"\x04", b"\xd2\x00", b"\x64\x00"])
    fake.read = lambda n=1: next(chunks, b"")  # type: ignore[assignment]
    assert transport.write(b"\x01\x02\x03\x04") == 4
    assert transport.frames_sent == 1
    timing = transport.last_timing
    assert timing is not None
    assert timing.draw_us == 1234
    assert timing.vectors == 100


def test_serial_transport_v2_surplus_bytes_kept_for_next_frame():
    # Two records arrive in one read; the second is buffered for the next frame.
    transport, fake = _make(flow_control=0x06, sync_timeout=0.5)
    transport._v2 = True
    reads = iter([b"\x04\xd2\x00\x64\x00\x00\x10\x00\x20\x02"])
    fake.read = lambda n=1: next(reads, b"")  # type: ignore[assignment]
    assert transport.write(b"\x01\x02\x03\x04") == 4
    assert transport.last_timing is not None
    assert transport.last_timing.draw_us == 1234
    # Second frame consumes the buffered record without another read.
    assert transport.write(b"\x05\x06\x07\x08") == 4
    assert transport.last_timing.draw_us == 0x0010
    assert transport.last_timing.vectors == 0x0020
    assert transport.last_timing.idle is True


def test_serial_transport_flow_control_auto_disables_without_ready():
    # FakeSerial.read returns zero bytes (never the 0x06): on timeout the handshake
    # is assumed absent (USB-CDC), so flow control disables and the frame streams.
    transport, _ = _make(flow_control=0x06, sync_timeout=0.02)
    assert transport.write(b"\x01\x02\x03\x04") == 4
    assert transport.flow_control is None


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
