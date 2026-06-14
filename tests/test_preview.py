"""Tests for the optional pyvterm.preview rendering module."""

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")

from pyvterm import VectorTerminal  # noqa: E402
from pyvterm.preview import PreviewTransport, decode_segments, rasterize  # noqa: E402


def test_decode_segments_round_trip():
    # A white (0,0)->(100,0) line decodes to one lit segment in device space.
    pt = PreviewTransport()
    vt = VectorTerminal(transport=pt)
    vt.set_intensity(15)
    vt.polyline([(0, 0), (100, 0)])
    frame = vt.send_frame()
    assert decode_segments(frame) == [(2049, 2050, 2449, 2050, 240)]


def test_blanked_moves_produce_no_segments():
    # With no colour set the beam is black, so nothing is lit.
    pt = PreviewTransport()
    vt = VectorTerminal(transport=pt)
    vt.polyline([(0, 0), (100, 0), (100, 100)])
    assert decode_segments(vt.send_frame()) == []


def test_rasterize_returns_rgb_image_of_given_size():
    from PIL.Image import Image

    img = rasterize([(0, 0, 4095, 4095, 255)], 80, 60)
    assert isinstance(img, Image)
    assert img.size == (80, 60)
    assert img.mode == "RGB"


def test_preview_transport_save_apng(tmp_path):
    from PIL import Image

    pt = PreviewTransport(width=64, height=48)
    vt = VectorTerminal(transport=pt)
    # Three clearly-distinct frames (Pillow merges byte-identical ones).
    shapes = [
        [(-400, -300), (400, 300)],
        [(-400, 300), (400, -300)],
        [(0, -300), (0, 300)],
    ]
    for shape in shapes:
        with vt.frame():
            vt.set_intensity(15)
            vt.polyline(shape)
    vt.close()  # writes an EXIT command that must NOT count as a frame

    out = tmp_path / "anim.png"
    assert pt.save_apng(str(out), fps=10) == 3
    with Image.open(out) as img:
        assert getattr(img, "n_frames", 1) == 3
        assert img.size == (64, 48)


def test_save_apng_with_no_frames_raises(tmp_path):
    pt = PreviewTransport()
    with pytest.raises(ValueError):
        pt.save_apng(str(tmp_path / "empty.png"))
