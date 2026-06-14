"""Tests for the 3D spectrum analyzer example.

The example lives in ``examples/`` (not an installed package), so it is loaded
by path. numpy is required for the analyzer and Pillow for the preview; both
are skipped if unavailable.
"""

import importlib.util
import pathlib

import pytest

pytest.importorskip("numpy")
import numpy as np  # noqa: E402

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "examples" / "spectrum3d.py"


def _load():
    spec = importlib.util.spec_from_file_location("spectrum3d", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spectrum3d = _load()


def test_synthetic_source_shape():
    source = spectrum3d.SyntheticSource(44100, 1024)
    frame = source.read_frame()
    assert frame.shape == (1024,)
    assert np.all(np.abs(frame) < 4.0)


def test_analyzer_outputs_normalised_levels():
    source = spectrum3d.SyntheticSource(44100, 1024)
    analyzer = spectrum3d.Analyzer(44100, 1024, n_bins=32)
    levels = analyzer.process(source.read_frame())
    assert levels.shape == (32,)
    assert levels.min() >= 0.0
    assert levels.max() <= 1.0


def test_waterfall_rows_and_depth_cue():
    waterfall = spectrum3d.Waterfall3D(n_bins=16, history=8)
    for _ in range(8):
        waterfall.push(np.linspace(0.0, 1.0, 16, dtype=np.float32))
    rows = list(waterfall.rows())
    assert len(rows) == 8
    assert all(len(points) == 16 for _, points in rows)
    # Front trace is brighter than the back trace.
    assert rows[0][0] > rows[-1][0]
    assert all(3 <= intensity <= 15 for intensity, _ in rows)


def test_main_dry_run_synthetic_returns_zero():
    assert spectrum3d.main(["--synthetic", "--dry-run", "--frames", "3", "--fps", "0"]) == 0


def test_preview_writes_animated_png(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    out = tmp_path / "preview.png"
    rc = spectrum3d.main(
        [
            "--synthetic",
            "--preview",
            str(out),
            "--frames",
            "4",
            "--width",
            "120",
            "--height",
            "90",
        ]
    )
    assert rc == 0
    assert out.exists()
    with Image.open(out) as img:
        assert getattr(img, "n_frames", 1) == 4
