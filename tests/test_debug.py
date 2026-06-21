"""Tests for the generic --debug telemetry (DebugReporter)."""

import argparse
import io

from pyvterm import DebugReporter, FrameTiming, MemoryTransport, VectorTerminal, debug


def test_add_debug_argument_parsing():
    p = argparse.ArgumentParser()
    debug.add_debug_argument(p)
    assert p.parse_args([]).debug is None
    assert p.parse_args(["--debug"]).debug == 1
    assert p.parse_args(["--debug", "10"]).debug == 10


def test_reporter_for_returns_none_when_off():
    vt = VectorTerminal(transport=MemoryTransport())
    assert debug.reporter_for(vt, None) is None
    assert debug.reporter_for(vt, 0) is None
    assert isinstance(debug.reporter_for(vt, 1), DebugReporter)


def _send(vt: VectorTerminal) -> None:
    vt.set_intensity(15)
    vt.draw_to(100, 0)
    vt.send_frame()


def test_reporter_emits_line_with_min_mean_max():
    clock = [0.0]
    mt = MemoryTransport()
    vt = VectorTerminal(transport=mt)
    out = io.StringIO()
    rep = DebugReporter(vt, period=1.0, out=out, clock=lambda: clock[0])

    # Three frames within the period: collected but not yet reported.
    for vectors, draw_us in [(100, 1000), (120, 2000), (140, 3000)]:
        _send(vt)
        mt.last_timing = FrameTiming(draw_us=draw_us, vectors=vectors, overflow=False, idle=False)
        rep.tick()
    assert out.getvalue() == ""

    # Crossing the period boundary emits exactly one line covering all samples.
    clock[0] = 1.5
    mt.last_timing = FrameTiming(draw_us=4000, vectors=160, overflow=False, idle=False)
    rep.tick()
    line = out.getvalue()
    assert line.count("\n") == 1
    assert "vectors min/mean/max=100/130.0/160" in line
    assert "draw_us min/mean/max=1000/2500.0/4000" in line
    assert "io=" in line and "bps" in line


def test_reporter_handles_no_device_timing():
    # No v2 device: last_timing stays None, so vector/draw stats are blank but the
    # I/O line still prints.
    clock = [0.0]
    vt = VectorTerminal(transport=MemoryTransport())
    out = io.StringIO()
    rep = DebugReporter(vt, period=1.0, out=out, clock=lambda: clock[0])
    _send(vt)
    rep.tick()
    clock[0] = 1.0
    rep.tick()
    line = out.getvalue()
    assert "vectors min/mean/max=-/-/-" in line
    assert "draw_us min/mean/max=-/-/-" in line
