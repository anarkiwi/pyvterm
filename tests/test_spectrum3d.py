"""Tests for the 3D spectrum analyzer example.

The example lives in ``examples/`` (not an installed package), so it is loaded
by path. numpy is required for the analyzer and Pillow for the preview; both
are skipped if unavailable.
"""

import importlib.util
import math
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


def test_waterfall_rows_follow_the_camera():
    waterfall = spectrum3d.Waterfall3D(n_bins=16, history=8)
    for _ in range(8):
        waterfall.push(np.linspace(0.0, 1.0, 16, dtype=np.float32))
    head_on = spectrum3d.Camera()
    swung = spectrum3d.Camera()
    swung.jump()  # move the base view off head-on
    for _ in range(60):
        swung.update()
    front_head_on = list(waterfall.rows(head_on))[0][1]
    front_swung = list(waterfall.rows(swung))[0][1]
    # Same geometry, different camera -> different projected points, all finite.
    assert front_head_on != front_swung
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in front_swung)


def test_change_detector_fires_once_then_respects_cooldown():
    det = spectrum3d.ChangeDetector(sensitivity=2.0, cooldown=4, floor=0.01, adapt=0.2, warmup=2)
    low = np.zeros(8, dtype=np.float32)
    assert det.update(low) is False  # first call only records the previous frame
    for _ in range(4):  # steady input clears warmup without ever firing
        assert det.update(low) is False
    high = np.ones(8, dtype=np.float32)
    assert det.update(high) is True  # a big rise in level is a major change
    # Cooldown blocks an immediate re-trigger even on another large change.
    assert det.update(low) is False
    assert det.update(high) is False


def test_change_detector_quiet_on_steady_input():
    det = spectrum3d.ChangeDetector()
    steady = np.full(16, 0.4, dtype=np.float32)
    fired = [det.update(steady) for _ in range(40)]
    assert not any(fired)


def test_camera_starts_settled_head_on():
    cam = spectrum3d.Camera()
    assert cam.yaw == pytest.approx(0.0, abs=1e-9)
    # Looking down a bit: a taller magnitude projects higher up the screen.
    _, base_y = cam.project(0.0, 0.0, 0.0)
    _, peak_y = cam.project(0.0, 0.5, 0.0)
    assert peak_y > base_y


def test_camera_jump_swings_toward_new_view():
    cam = spectrum3d.Camera()
    cam.jump()  # advance to preset 1
    assert (cam.base_yaw, cam.base_pitch) == spectrum3d.VIEW_PRESETS[1]
    before = cam.yaw
    cam.update()
    assert cam.yaw > before  # eases toward the new (positive-yaw) target
    for _ in range(200):  # converges to track the swaying target near the base view
        cam.update()
    assert abs(cam.yaw - cam.base_yaw) <= cam.sway_yaw + 0.05


def test_step_drives_camera_and_records_jumps():
    source = spectrum3d.SyntheticSource(44100, 1024)
    analyzer = spectrum3d.Analyzer(44100, 1024, n_bins=16)
    waterfall = spectrum3d.Waterfall3D(n_bins=16, history=8)
    camera = spectrum3d.Camera()
    detector = spectrum3d.ChangeDetector()
    jumps = sum(
        spectrum3d.step(source, analyzer, waterfall, camera, detector, rotate=True)
        for _ in range(120)
    )
    assert jumps >= 1  # the lively synthetic signal should trip at least one change
    assert len(list(waterfall.rows(camera))) == 8


def test_step_no_rotate_keeps_camera_fixed():
    source = spectrum3d.SyntheticSource(44100, 1024)
    analyzer = spectrum3d.Analyzer(44100, 1024, n_bins=16)
    waterfall = spectrum3d.Waterfall3D(n_bins=16, history=8)
    camera = spectrum3d.Camera()
    detector = spectrum3d.ChangeDetector()
    for _ in range(50):
        spectrum3d.step(source, analyzer, waterfall, camera, detector, rotate=False)
    assert camera.yaw == 0.0 and camera.pitch == spectrum3d.VIEW_PRESETS[0][1]


def test_main_no_rotate_runs():
    rc = spectrum3d.main(["--synthetic", "--dry-run", "--frames", "3", "--fps", "0", "--no-rotate"])
    assert rc == 0


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
