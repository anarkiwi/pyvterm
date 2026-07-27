"""Tests for the high-level VectorTerminal using an in-memory transport."""

import pytest

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


class _FakeClock:
    """Deterministic ``time`` stand-in: sleeping advances the clock."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        assert d >= 0
        self.slept.append(d)
        self.t += d

    def work(self, d: float) -> None:
        self.t += d


def _clock(monkeypatch) -> _FakeClock:
    clk = _FakeClock()
    monkeypatch.setattr(terminal_mod.time, "monotonic", clk.monotonic)
    monkeypatch.setattr(terminal_mod.time, "sleep", clk.sleep)
    return clk


def test_pace_numeric_floors_the_frame_period(monkeypatch):
    # The deadline pacer floors the *period* at 1/fps; the first call only sets
    # the baseline, so the sleep lands once a prior frame exists.
    clk = _clock(monkeypatch)
    vt = VectorTerminal(transport=MemoryTransport())
    vt.pace(50.0)  # baseline, no sleep
    clk.work(0.005)  # 5 ms of frame work
    vt.pace(50.0)
    assert clk.slept == [pytest.approx(1.0 / 50.0 - 0.005)]  # sleep tops up to 20 ms


def test_pace_auto_uses_draw_time_even_under_flow_control(monkeypatch):
    # The handshake only signals receive-readiness; draw_us paces us to the beam's
    # real draw rate so a heavy scene can't outrun it. This is honoured *with* flow
    # control on (the case that previously ignored draw_us and let the display lag).
    clk = _clock(monkeypatch)
    mt = MemoryTransport()
    mt.flow_control = 0x06  # type: ignore[attr-defined]
    mt.last_timing = FrameTiming(draw_us=20_000, vectors=10, overflow=False, idle=False)
    vt = VectorTerminal(transport=mt)
    vt.pace(None)  # baseline
    vt.pace(None)  # ~0 work -> sleep a full draw period
    assert clk.slept == [pytest.approx(20_000 / 1_000_000)]


def test_pace_auto_never_slows_a_lockstep_receiver(monkeypatch):
    # If the loop already spends longer than draw_us (e.g. a genuinely lockstep
    # handshake), no extra sleep is added -- the period is not double-charged.
    clk = _clock(monkeypatch)
    mt = MemoryTransport()
    mt.last_timing = FrameTiming(draw_us=20_000, vectors=10, overflow=False, idle=False)
    vt = VectorTerminal(transport=mt)
    vt.pace(None)  # baseline
    clk.work(0.050)  # 50 ms of work > 20 ms target
    vt.pace(None)
    assert clk.slept == []


def test_pace_auto_no_timing_adds_no_sleep(monkeypatch):
    # With no reported draw time there is nothing to pace to.
    clk = _clock(monkeypatch)
    mt = MemoryTransport()
    mt.flow_control = 0x06  # type: ignore[attr-defined]
    vt = VectorTerminal(transport=mt)
    vt.pace(None)
    vt.pace(None)
    assert clk.slept == []


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
