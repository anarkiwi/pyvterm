"""Tests for the Rutt-Etra video example.

Loaded by path (it lives in ``examples/``). numpy is required; the OpenCV
reader is exercised with an injected fake capture, so real OpenCV is optional.
"""

import importlib.util
import pathlib

import pytest

pytest.importorskip("numpy")
import numpy as np  # noqa: E402

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "examples" / "ruttetra.py"


def _load():
    spec = importlib.util.spec_from_file_location("ruttetra", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ruttetra = _load()


def test_synthetic_source_returns_normalised_frame():
    frame = ruttetra.SyntheticVideoSource(160, 120).read()
    assert frame is not None
    assert frame.shape == (120, 160)
    assert float(frame.min()) >= 0.0 and float(frame.max()) <= 1.0


def test_scanlines_full_grid_without_threshold():
    proc = ruttetra.RuttEtra(cols=40, rows=24, threshold=0.0)
    frame = ruttetra.SyntheticVideoSource().read()
    runs = proc.scanlines(frame)
    # One run per scan line, each with `cols` points.
    assert len(runs) == 24
    assert all(len(run) == 40 for run in runs)
    # Everything lands inside the Vectrex bounds (no clipping needed).
    xs = [x for run in runs for x, _ in run]
    ys = [y for run in runs for _, y in run]
    assert max(abs(x) for x in xs) <= ruttetra.X_HALF + 1
    assert min(ys) >= ruttetra.Y_BOTTOM - 1
    assert max(ys) <= ruttetra.Y_TOP + 95 + 1


def test_threshold_splits_and_reduces_points():
    frame = ruttetra.SyntheticVideoSource().read()
    full = ruttetra.RuttEtra(cols=40, rows=24, threshold=0.0).scanlines(frame)
    gated = ruttetra.RuttEtra(cols=40, rows=24, threshold=0.5).scanlines(frame)
    full_points = sum(len(r) for r in full)
    gated_points = sum(len(r) for r in gated)
    assert gated_points < full_points  # dark areas dropped


def test_draw_reports_vector_count():
    from pyvterm import MemoryTransport, VectorTerminal

    vt = VectorTerminal(transport=MemoryTransport())
    proc = ruttetra.RuttEtra(cols=30, rows=20, threshold=0.0)
    frame = ruttetra.SyntheticVideoSource().read()
    with vt.frame():
        vectors = proc.draw(vt, frame)
    assert vectors == 20 * (30 - 1)


class _FakeCapture:
    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def read(self):
        if self._i < len(self._frames):
            frame = self._frames[self._i]
            self._i += 1
            return True, frame
        return False, None

    def get(self, _prop):
        return 0.0  # unknown source fps -> no frame skipping

    def release(self):
        pass


def test_cv2_source_reads_frames_then_stops():
    captured = {}

    frames = [np.full((4, 6, 3), v, dtype=np.uint8) for v in (10, 128, 240)]

    def factory(src):
        captured["src"] = src
        return _FakeCapture(frames)

    source = ruttetra.Cv2VideoSource("0", capture_factory=factory)
    assert captured["src"] == 0  # numeric string -> camera index

    first = source.read()
    assert first is not None
    assert first.shape == (4, 6)
    assert 0.0 <= float(first.min()) <= float(first.max()) <= 1.0
    assert source.read() is not None
    assert source.read() is not None
    assert source.read() is None  # exhausted


def test_main_dry_run_synthetic_returns_zero():
    assert ruttetra.main(["--synthetic", "--dry-run", "--frames", "3", "--fps", "0"]) == 0


def test_preview_writes_animated_png(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    out = tmp_path / "ruttetra.png"
    rc = ruttetra.main(
        ["--synthetic", "--preview", str(out), "--frames", "4", "--width", "120", "--height", "90"]
    )
    assert rc == 0
    with Image.open(out) as img:
        assert getattr(img, "n_frames", 1) == 4
