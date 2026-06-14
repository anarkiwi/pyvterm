"""Tests for the stateful FrameBuilder, including exact frame bytes."""

from pyvterm import protocol
from pyvterm.frame import FrameBuilder
from pyvterm.protocol import Flag

# A single white vector from host (0,0) to (100,0), assembled by hand:
FRAME_HEADER = bytes.fromhex("80000190")  # FRAME, length 400
RGB_WHITE = bytes.fromhex("20f0f0f0")  # RGB 240,240,240
MOVE_TO_START = bytes.fromhex("52004802")  # XY blank to (2049, 2050)
DRAW_TO_END = bytes.fromhex("42644802")  # XY lit to (2449, 2050)
QUALITY = bytes.fromhex("60000005")  # QUALITY 5
COMPLETE = bytes.fromhex("00000000")  # COMPLETE

EXPECTED = FRAME_HEADER + RGB_WHITE + MOVE_TO_START + DRAW_TO_END + QUALITY + COMPLETE
EMPTY_FRAME = bytes.fromhex("80000000") + QUALITY + COMPLETE


def words(data: bytes) -> list[int]:
    return [int.from_bytes(data[i : i + 4], "big") for i in range(0, len(data), 4)]


def test_single_white_vector_exact_bytes():
    fb = FrameBuilder()
    fb.set_rgb(15, 15, 15)
    assert fb.vector(0, 0, 100, 0) is True
    assert fb.vector_count == 1
    assert fb.total_length == 400
    assert fb.to_bytes() == EXPECTED


def test_uncolored_vector_is_blanked():
    # With no colour set the builder starts black, so even the "draw" is blanked.
    fb = FrameBuilder()
    fb.vector(0, 0, 100, 0)
    xy_words = [protocol.decode_word(w) for w in words(fb.to_bytes())]
    draws = [w for w in xy_words if w["flag"] is Flag.XY]
    assert len(draws) == 2  # reposition + "draw"
    assert all(w["blank"] for w in draws)


def test_offscreen_vector_emits_nothing():
    fb = FrameBuilder()
    assert fb.vector(1000, 1000, 2000, 2000) is False
    assert fb.vector_count == 0
    assert fb.to_bytes() == EMPTY_FRAME


def test_connected_polyline_uses_single_reposition():
    fb = FrameBuilder()
    fb.set_rgb(15, 15, 15)
    emitted = fb.polyline([(0, 0), (100, 0), (100, 100)])
    assert emitted == 2
    assert fb.vector_count == 2
    decoded = [protocol.decode_word(w) for w in words(fb.to_bytes())]
    xys = [w for w in decoded if w["flag"] is Flag.XY]
    # One reposition (blank) + two lit draws — the shared vertex is not repositioned.
    assert len(xys) == 3
    assert sum(1 for w in xys if w["blank"]) == 1


def test_closed_polyline_adds_a_segment():
    fb = FrameBuilder()
    fb.set_rgb(15, 15, 15)
    emitted = fb.polyline([(0, 0), (100, 0), (100, 100)], closed=True)
    assert emitted == 3
    assert fb.vector_count == 3


def test_reset_clears_everything():
    fb = FrameBuilder()
    fb.set_rgb(15, 15, 15)
    fb.vector(0, 0, 100, 0)
    fb.reset()
    assert fb.vector_count == 0
    assert fb.total_length == 0
    assert fb.to_bytes() == EMPTY_FRAME


def test_clip_window_rejects_outside():
    fb = FrameBuilder()
    fb.set_clip_window(-10, -10, 10, 10)
    fb.set_rgb(15, 15, 15)
    assert fb.vector(100, 100, 200, 200) is False
    assert fb.vector_count == 0
