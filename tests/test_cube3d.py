"""Tests for the 3D rotating cube example.

Loaded by path (it lives in ``examples/``). The example itself is pure-math
and needs no extras; only the preview test requires Pillow.
"""

import importlib.util
import pathlib

import pytest

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "examples" / "cube3d.py"


def _load():
    spec = importlib.util.spec_from_file_location("cube3d", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cube3d = _load()


def _bbox_span(points):
    """Width + height of the bounding box of a list of (x, y) points."""
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def _centroid(points):
    n = len(points)
    return (sum(x for x, _ in points) / n, sum(y for _, y in points) / n)


def test_project_returns_eight_points():
    points = cube3d.SpinningCube().project(0)
    assert len(points) == 8
    assert all(len(p) == 2 for p in points)


def test_edges_count_and_endpoints():
    edges = cube3d.SpinningCube().edges(7)
    assert len(edges) == 12
    # Every edge endpoint is one of the eight projected vertices.
    points = set(cube3d.SpinningCube().project(7))
    for start, end in edges:
        assert start in points and end in points


def test_distance_oscillates_around_base():
    cube = cube3d.SpinningCube(distance=6.0, zoom=0.4)
    distances = [cube.distance_at(f) for f in range(cube.period)]
    assert min(distances) == pytest.approx(6.0 * 0.6, rel=1e-3)
    assert max(distances) == pytest.approx(6.0 * 1.4, rel=1e-3)


def test_moving_in_makes_the_cube_bigger():
    """The nearest point in the loop must project larger than the farthest."""
    cube = cube3d.SpinningCube()
    frames = range(cube.period)
    near = min(frames, key=cube.distance_at)  # closest -> biggest
    far = max(frames, key=cube.distance_at)  # farthest -> smallest
    assert _bbox_span(cube.project(near)) > _bbox_span(cube.project(far))


def test_roam_moves_the_cube_around_the_screen():
    cube = cube3d.SpinningCube()
    # Sample the centroid across the loop; it should sweep a real 2D region.
    centroids = [_centroid(cube.project(f)) for f in range(0, cube.period, 5)]
    xs = [cx for cx, _ in centroids]
    ys = [cy for _, cy in centroids]
    assert max(xs) - min(xs) > cube.roam_x  # wanders horizontally...
    assert max(ys) - min(ys) > cube.roam_y  # ...and vertically


def test_zoom_zero_keeps_constant_distance():
    cube = cube3d.SpinningCube(zoom=0.0)
    assert all(cube.distance_at(f) == pytest.approx(cube.distance) for f in range(cube.period))


def test_draw_reports_edge_count():
    from pyvterm import MemoryTransport, VectorTerminal

    vt = VectorTerminal(transport=MemoryTransport())
    cube = cube3d.SpinningCube()
    with vt.frame():
        drawn = cube.draw(vt, 3)
    assert drawn == 12


def test_main_dry_run_returns_zero():
    assert cube3d.main(["--dry-run", "--frames", "3", "--fps", "0"]) == 0


def test_preview_writes_animated_png(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    out = tmp_path / "cube3d.png"
    rc = cube3d.main(["--preview", str(out), "--frames", "4", "--width", "120", "--height", "90"])
    assert rc == 0
    assert out.exists()
    with Image.open(out) as img:
        assert getattr(img, "n_frames", 1) == 4
