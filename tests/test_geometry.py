"""Tests for clipping and vector-length helpers."""

from pyvterm.geometry import clip_line, vector_length

WINDOW = (-512, -384, 511, 383)


def test_vector_length():
    assert vector_length(0, 0, 3, 4) == 5
    assert vector_length(0, 0, 400, 0) == 400
    assert vector_length(10, 10, 10, 10) == 0


def test_clip_line_fully_inside_is_unchanged():
    assert clip_line(-100, -100, 100, 100, WINDOW) == (-100, -100, 100, 100)


def test_clip_line_fully_outside_returns_none():
    # Both endpoints to the right of the window.
    assert clip_line(1000, 0, 2000, 0, WINDOW) is None
    # Both above.
    assert clip_line(0, 1000, 0, 2000, WINDOW) is None


def test_clip_line_crossing_right_edge():
    clipped = clip_line(0, 0, 1000, 0, WINDOW)
    assert clipped is not None
    x1, y1, x2, y2 = clipped
    assert (x1, y1) == (0, 0)
    assert x2 == 511  # clipped to the right edge
    assert y2 == 0


def test_clip_line_crossing_two_edges():
    # The diagonal y = x leaves the window through the horizontal edges first
    # (y reaches +/-384 while |x| is still < 512).
    assert clip_line(-1000, -1000, 1000, 1000, WINDOW) == (-384, -384, 383, 383)
