#!/usr/bin/env python3
"""A 3D wireframe cube flying around a Vectrex via pyvterm.

The cube does three things at once, which is exactly the kind of motion a
vector display renders beautifully:

* **Tumbles** on all three axes (a different rotation rate per axis, so it
  never settles into an obvious loop).
* **Roams** across the screen on a Lissajous path (X and Y drift at different
  rates, tracing a wandering figure).
* **Moves in and out**: its distance from the viewer swings toward and away,
  and perspective projection makes it loom large up close and shrink into the
  distance.

The maths is plain trigonometry — eight corners rotated by three angles, then
divided by depth for perspective — so this example needs **only the core
package** (no numpy). The optional ``--preview`` path additionally needs the
``preview`` extra to rasterise the animated PNG.

Everything is driven by a single loop ``--period`` (in frames). Rotation,
roam, and distance all complete a whole number of cycles per period, so the
motion repeats seamlessly — the ``--preview`` animation loops without a seam.

Examples
--------
Run on real hardware (USB-DVG / PiTrex on /dev/ttyACM0)::

    python examples/cube3d.py --port /dev/ttyACM0

Without any hardware (prints per-frame byte/vector counts and the distance)::

    python examples/cube3d.py --dry-run --frames 5

Render the animated PNG (no hardware; needs the preview extra)::

    pip install "pyvterm[preview]"
    python examples/cube3d.py --preview cube3d.png
"""

from __future__ import annotations

import argparse
import math
import time

from pyvterm import DEFAULT_BAUDRATE, DEFAULT_PORT, MemoryTransport, VectorTerminal

# The eight corners of a cube centred on the origin, half-width 1.
VERTICES: list[tuple[float, float, float]] = [
    (-1.0, -1.0, -1.0),
    (1.0, -1.0, -1.0),
    (1.0, 1.0, -1.0),
    (-1.0, 1.0, -1.0),
    (-1.0, -1.0, 1.0),
    (1.0, -1.0, 1.0),
    (1.0, 1.0, 1.0),
    (-1.0, 1.0, 1.0),
]

# The twelve edges as pairs of vertex indices, grouped by the cube's two z
# faces and the four pillars joining them. Tracing each face loop contiguously
# lets the beam draw a square without lifting, saving blanked repositions.
_BACK_FACE = [(0, 1), (1, 2), (2, 3), (3, 0)]  # z = -1
_FRONT_FACE = [(4, 5), (5, 6), (6, 7), (7, 4)]  # z = +1
_PILLARS = [(0, 4), (1, 5), (2, 6), (3, 7)]  # joining the faces
EDGES: list[tuple[int, int]] = _BACK_FACE + _FRONT_FACE + _PILLARS

# Whole-number cycles completed per loop period, so the animation is seamless.
SPIN_CYCLES: tuple[float, float, float] = (2.0, 3.0, 1.0)  # rotations per axis
ROAM_CYCLES: tuple[float, float] = (1.0, 2.0)  # 1:2 Lissajous screen wander
DIST_CYCLES: float = 1.0  # one move out-and-back per loop


def _rotate(
    point: tuple[float, float, float], ax: float, ay: float, az: float
) -> tuple[float, float, float]:
    """Rotate a 3D point by ``ax``, ``ay``, ``az`` radians about X, then Y, then Z."""
    x, y, z = point
    ca, sa = math.cos(ax), math.sin(ax)
    y, z = y * ca - z * sa, y * sa + z * ca
    cb, sb = math.cos(ay), math.sin(ay)
    x, z = x * cb + z * sb, -x * sb + z * cb
    cc, sc = math.cos(az), math.sin(az)
    x, y = x * cc - y * sc, x * sc + y * cc
    return x, y, z


class SpinningCube:
    """Project the tumbling, roaming, looming cube to screen coordinates.

    Parameters
    ----------
    size:
        Apparent half-size (host units) of the cube at the base distance.
    distance:
        Base camera distance in cube half-widths. Larger flattens the
        perspective; the focal length scales with it so ``size`` stays fixed.
    zoom:
        In/out travel as a fraction of ``distance`` (``0`` = no in/out, the
        cube sits at a constant depth). The depth swings by ``±zoom`` of base.
    roam_x, roam_y:
        Screen-space roam amplitude (host units) for the wandering path.
    period:
        Frames per full animation loop (smaller = faster motion).
    intensity:
        Beam brightness, 0 (off) .. 15 (brightest).
    """

    def __init__(
        self,
        *,
        size: float = 120.0,
        distance: float = 6.0,
        zoom: float = 0.4,
        roam_x: float = 180.0,
        roam_y: float = 120.0,
        period: int = 120,
        intensity: int = 15,
    ) -> None:
        self.size = size
        self.distance = distance
        self.zoom = zoom
        self.roam_x = roam_x
        self.roam_y = roam_y
        self.period = max(1, period)
        self.intensity = intensity
        # Focal length so a unit coordinate maps to `size` host units at the
        # base distance (perspective divide cancels `distance` there).
        self.focal = size * distance

    def distance_at(self, frame: int) -> float:
        """Camera distance of the cube centre at ``frame`` (the in/out swing)."""
        u = frame / self.period
        return self.distance * (1.0 + self.zoom * math.sin(2.0 * math.pi * DIST_CYCLES * u))

    def project(self, frame: int) -> list[tuple[float, float]]:
        """Return the eight cube corners projected to host (X, Y) at ``frame``."""
        u = frame / self.period
        tau = 2.0 * math.pi
        ax = tau * SPIN_CYCLES[0] * u
        ay = tau * SPIN_CYCLES[1] * u
        az = tau * SPIN_CYCLES[2] * u
        center_z = self.distance_at(frame)
        roam_x = self.roam_x * math.sin(tau * ROAM_CYCLES[0] * u)
        roam_y = self.roam_y * math.sin(tau * ROAM_CYCLES[1] * u)

        points: list[tuple[float, float]] = []
        for vertex in VERTICES:
            rx, ry, rz = _rotate(vertex, ax, ay, az)
            z = max(center_z + rz, 0.1)  # keep corners in front of the camera
            points.append((self.focal * rx / z + roam_x, self.focal * ry / z + roam_y))
        return points

    def edges(self, frame: int) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Return the twelve projected edges as ``(start, end)`` point pairs."""
        points = self.project(frame)
        return [(points[i], points[j]) for i, j in EDGES]

    def draw(self, terminal: VectorTerminal, frame: int) -> int:
        """Draw the cube into the terminal's frame; returns the edge count."""
        points = self.project(frame)
        terminal.set_intensity(self.intensity)
        for i, j in EDGES:
            terminal.vector(points[i][0], points[i][1], points[j][0], points[j][1])
        return len(EDGES)


# --- CLI ------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fly a 3D wireframe cube around a Vectrex via pyvterm.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    cube = parser.add_argument_group("cube motion")
    cube.add_argument("--size", type=float, default=120.0, help="apparent half-size (host units)")
    cube.add_argument(
        "--distance", type=float, default=6.0, help="base camera distance (cube half-widths)"
    )
    cube.add_argument(
        "--zoom", type=float, default=0.4, help="in/out travel, fraction of distance (0 = none)"
    )
    cube.add_argument("--roam-x", type=float, default=180.0, help="screen roam amplitude, X")
    cube.add_argument("--roam-y", type=float, default=120.0, help="screen roam amplitude, Y")
    cube.add_argument("--period", type=int, default=120, help="frames per loop (smaller = faster)")
    cube.add_argument("--intensity", type=int, default=15, help="beam brightness 0-15")

    out = parser.add_argument_group("output")
    out.add_argument("--port", default=DEFAULT_PORT, help="serial device path")
    out.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help="nominal baud rate")
    out.add_argument("--fps", type=float, default=30.0, help="target frames per second")
    out.add_argument("--frames", type=int, default=0, help="frames to run (0 = forever)")
    out.add_argument("--dry-run", action="store_true", help="don't open a serial port")
    out.add_argument("--preview", metavar="OUT.png", help="render an animated PNG and exit")
    out.add_argument("--width", type=int, default=480, help="preview width (px)")
    out.add_argument("--height", type=int, default=360, help="preview height (px)")
    return parser.parse_args(argv)


def make_cube(args: argparse.Namespace) -> SpinningCube:
    return SpinningCube(
        size=args.size,
        distance=args.distance,
        zoom=args.zoom,
        roam_x=args.roam_x,
        roam_y=args.roam_y,
        period=args.period,
        intensity=args.intensity,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cube = make_cube(args)

    if args.preview:
        from pyvterm.preview import PreviewTransport

        # One full period loops seamlessly; let --frames override the length.
        n_frames = args.frames or cube.period
        terminal = VectorTerminal(transport=PreviewTransport(width=args.width, height=args.height))
        print(f"Rendering {n_frames} frames to {args.preview} ...")
        for frame in range(n_frames):
            with terminal.frame():
                cube.draw(terminal, frame)
        saved = terminal.transport.save_apng(args.preview, fps=args.fps)  # type: ignore[attr-defined]
        print(f"Wrote {args.preview} ({saved} frames, {args.width}x{args.height})")
        return 0

    if args.dry_run:
        terminal = VectorTerminal(transport=MemoryTransport())
        print("[dry run] no serial port opened")
    else:
        print(f"Opening {args.port} at {args.baud} baud (waiting for the device to settle)...")
        terminal = VectorTerminal(port=args.port, baudrate=args.baud)

    period = 1.0 / args.fps if args.fps > 0 else 0.0
    drawn = 0
    try:
        while args.frames == 0 or drawn < args.frames:
            with terminal.frame():
                vectors = cube.draw(terminal, drawn)
            if args.dry_run:
                last = terminal.transport.frames[-1]  # type: ignore[attr-defined]
                print(
                    f"frame {drawn:>4}: {len(last):>5} bytes, "
                    f"{vectors} vectors, dist={cube.distance_at(drawn):4.1f}"
                )
            drawn += 1
            if period:
                time.sleep(period)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        terminal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
