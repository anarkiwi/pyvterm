"""Tests for the high-level VectorTerminal using an in-memory transport."""

import pyvterm.terminal as terminal_mod
from pyvterm import FrameTiming, MemoryTransport, VectorTerminal, protocol

# Same hand-assembled frame as test_frame.py.
EXPECTED = (
    bytes.fromhex("80000190")
    + bytes.fromhex("20f0f0f0")
    + bytes.fromhex("52004802")
    + bytes.fromhex("42644802")
    + bytes.fromhex("60000005")
    + bytes.fromhex("00000000")
)
EMPTY_FRAME = bytes.fromhex("80000000") + bytes.fromhex("60000005") + bytes.fromhex("00000000")


def test_frame_context_sends_expected_bytes():
    mt = MemoryTransport()
    vt = VectorTerminal(transport=mt)
    with vt.frame():
        vt.set_intensity(15)
        vt.draw_to(100, 0)  # pen starts at (0, 0)
    assert mt.getvalue() == EXPECTED


def test_send_frame_returns_bytes_then_resets():
    mt = MemoryTransport()
    vt = VectorTerminal(transport=mt)
    vt.set_intensity(15)
    vt.draw_to(100, 0)
    assert vt.send_frame() == EXPECTED
    # After sending, the builder is reset: the next frame is empty.
    assert vt.send_frame() == EMPTY_FRAME


def test_send_keepalive_writes_keepalive_word():
    mt = MemoryTransport()
    vt = VectorTerminal(transport=mt)
    assert vt.send_keepalive() == protocol.keepalive()
    assert mt.getvalue() == protocol.keepalive()
    # A keepalive must not poison duplicate-suppression of real frames.
    assert vt._last_sent is None


def test_last_timing_defaults_to_none():
    vt = VectorTerminal(transport=MemoryTransport())
    assert vt.last_timing is None


def test_pace_numeric_sleeps_one_over_fps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(terminal_mod.time, "sleep", calls.append)
    vt = VectorTerminal(transport=MemoryTransport())
    vt.pace(50.0)
    assert calls == [1.0 / 50.0]


def test_pace_auto_adds_no_sleep_under_flow_control(monkeypatch):
    # An active handshake already paces the loop, so auto adds no sleep.
    calls: list[float] = []
    monkeypatch.setattr(terminal_mod.time, "sleep", calls.append)
    mt = MemoryTransport()
    mt.flow_control = 0x06  # type: ignore[attr-defined]
    vt = VectorTerminal(transport=mt)
    vt.pace(None)
    assert calls == []


def test_pace_auto_falls_back_to_draw_time_without_back_pressure(monkeypatch):
    # No flow control: auto pacing uses the device-reported draw time.
    calls: list[float] = []
    monkeypatch.setattr(terminal_mod.time, "sleep", calls.append)
    mt = MemoryTransport()
    mt.flow_control = None  # type: ignore[attr-defined]
    mt.last_timing = FrameTiming(draw_us=20_000, vectors=10, overflow=False, idle=False)
    vt = VectorTerminal(transport=mt)
    vt.pace(None)
    assert calls == [20_000 / 1_000_000]


def test_close_sends_exit_and_closes_transport():
    mt = MemoryTransport()
    vt = VectorTerminal(transport=mt)
    vt.close()
    assert mt.getvalue() == protocol.exit_command()
    assert mt.closed is True


def test_context_manager_closes():
    mt = MemoryTransport()
    with VectorTerminal(transport=mt):
        pass
    assert mt.closed is True
    assert mt.getvalue() == protocol.exit_command()


def test_move_to_then_draw_to():
    mt = MemoryTransport()
    vt = VectorTerminal(transport=mt)
    vt.move_to(50, 50)
    vt.draw_to(60, 60)
    assert vt.builder.vector_count == 1


def test_polyline_closed_counts_segments():
    mt = MemoryTransport()
    vt = VectorTerminal(transport=mt)
    vt.set_intensity(15)
    vt.polyline([(0, 0), (100, 0), (100, 100)], closed=True)
    assert vt.builder.vector_count == 3


def test_fluent_methods_return_self():
    vt = VectorTerminal(transport=MemoryTransport())
    assert vt.set_intensity(15) is vt
    assert vt.set_rgb(1, 2, 3) is vt
    assert vt.move_to(0, 0) is vt
    assert vt.draw_to(1, 1) is vt
    assert vt.vector(0, 0, 1, 1) is vt
    assert vt.set_clip_window(-1, -1, 1, 1) is vt
    assert vt.clear() is vt
