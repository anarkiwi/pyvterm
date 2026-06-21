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
  distance — and its beam **brightens up close and dims as it recedes**.
* **Flies over a rippling floor**: a perspective grid below the cube ripples
  with a slow travelling wave and fades into the background as it recedes
  (disable it with ``--no-floor``).

The maths is plain trigonometry — eight corners rotated by three angles, then
divided by depth for perspective — so this example needs **only the core
package** (no numpy). The optional ``--preview`` path additionally needs the
``preview`` extra to rasterise the animated PNG.

Everything is driven by a single loop ``--period`` (in frames). Rotation,
roam, and distance all complete a whole number of cycles per period, so the
motion repeats seamlessly — the ``--preview`` animation loops without a seam.

Examples
--------
Run on real hardware (USB-DVG / PiTrex on /dev/ttyUSB0)::

    python examples/cube3d.py --port /dev/ttyUSB0

Without any hardware (prints per-frame byte/vector counts and the distance)::

    python examples/cube3d.py --dry-run --frames 5

Render the animated PNG (no hardware; needs the preview extra)::

    pip install "pyvterm[preview]"
    python examples/cube3d.py --preview cube3d.png
"""

from __future__ import annotations

import argparse
import math

from pyvterm import DEFAULT_PORT, MemoryTransport, VectorTerminal, debug

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

    def brightness_at(self, frame: int) -> int:
        """Beam intensity for ``frame``, scaled by apparent size (depth cue).

        The cube swings toward and away from the camera; it should glow brighter
        up close (large) and fade as it recedes (small). Maps the distance across
        its in/out range onto ``[dim, intensity]``.
        """
        near, far = self.distance * (1.0 - self.zoom), self.distance * (1.0 + self.zoom)
        frac = 0.0 if far <= near else (self.distance_at(frame) - near) / (far - near)
        frac = min(1.0, max(0.0, frac))  # 0 = nearest/brightest, 1 = farthest/dimmest
        dim = max(3, round(self.intensity * 0.3))
        return round(self.intensity - frac * (self.intensity - dim))

    def draw(self, terminal: VectorTerminal, frame: int) -> int:
        """Draw the cube into the terminal's frame; returns the edge count."""
        points = self.project(frame)
        terminal.set_intensity(self.brightness_at(frame))
        for i, j in EDGES:
            terminal.vector(points[i][0], points[i][1], points[j][0], points[j][1])
        return len(EDGES)


class RippleFloor:
    """A perspective grid below the cube that ripples and fades into the distance.

    Horizontal contour lines lie on a floor plane and recede from the bottom of
    the screen toward a horizon near centre; a slow travelling sine wave ripples
    their height, and each row dims with depth so the far edge fades into the
    background. It shares the cube's focal length so the cube reads as flying
    *over* it.
    """

    def __init__(
        self,
        focal: float,
        *,
        cols: int = 9,
        rows: int = 6,
        width: float = 1.5,
        base_y: float = -1.0,
        near: float = 3.0,
        far: float = 22.0,
        amplitude: float = 0.14,
        wave_x: float = 2.2,
        wave_z: float = 0.55,
        speed: float = 0.06,
        near_intensity: int = 9,
        far_intensity: int = 1,
    ) -> None:
        self.focal = focal
        self.cols = max(2, cols)
        self.rows = max(2, rows)
        self.width = width
        self.base_y = base_y
        self.near = near
        self.far = far
        self.amplitude = amplitude
        self.wave_x = wave_x
        self.wave_z = wave_z
        self.speed = speed
        self.near_intensity = near_intensity
        self.far_intensity = far_intensity

    def draw(self, terminal: VectorTerminal, frame: int) -> int:
        """Draw the rippling, depth-fading floor; returns the segment count."""
        phase = frame * self.speed
        segments = 0
        for r in range(self.rows):
            frac = r / (self.rows - 1)  # 0 = nearest row, 1 = farthest
            depth = self.near + (self.far - self.near) * frac
            intensity = round(
                self.near_intensity + (self.far_intensity - self.near_intensity) * frac
            )
            points: list[tuple[float, float]] = []
            for c in range(self.cols):
                x = (c / (self.cols - 1) - 0.5) * 2.0 * self.width
                y = self.base_y + self.amplitude * math.sin(
                    self.wave_x * x + self.wave_z * depth + phase
                )
                points.append((self.focal * x / depth, self.focal * y / depth))
            terminal.set_intensity(max(1, intensity))
            terminal.polyline(points)
            segments += self.cols - 1
        return segments


def draw_scene(
    terminal: VectorTerminal, cube: SpinningCube, floor: RippleFloor | None, frame: int
) -> int:
    """Draw the floor (if any) then the cube into the current frame.

    The floor is drawn first so the brighter cube overlays it; returns the total
    vector count.
    """
    vectors = floor.draw(terminal, frame) if floor is not None else 0
    vectors += cube.draw(terminal, frame)
    return vectors


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
    cube.add_argument(
        "--no-floor", action="store_true", help="hide the rippling floor mesh under the cube"
    )

    out = parser.add_argument_group("output")
    out.add_argument("--port", default=DEFAULT_PORT, help="serial device path")
    out.add_argument(
        "--baud",
        default="auto",
        help="line rate, or 'auto' (default) to detect the receiver's baud",
    )
    out.add_argument(
        "--fps",
        default="auto",
        help="target frames per second, or 'auto' (default) to let the device pace the stream",
    )
    out.add_argument("--frames", type=int, default=0, help="frames to run (0 = forever)")
    out.add_argument("--dry-run", action="store_true", help="don't open a serial port")
    out.add_argument("--preview", metavar="OUT.png", help="render an animated PNG and exit")
    out.add_argument("--width", type=int, default=480, help="preview width (px)")
    out.add_argument("--height", type=int, default=360, help="preview height (px)")
    debug.add_debug_argument(out)
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
    # fps "auto" -> None (the device paces the stream); else a numeric target.
    fps = None if str(args.fps).lower() == "auto" else float(args.fps)
    cube = make_cube(args)
    floor = None if args.no_floor else RippleFloor(cube.focal)

    if args.preview:
        from pyvterm.preview import PreviewTransport

        # One full period loops seamlessly; let --frames override the length.
        n_frames = args.frames or cube.period
        terminal = VectorTerminal(transport=PreviewTransport(width=args.width, height=args.height))
        print(f"Rendering {n_frames} frames to {args.preview} ...")
        for frame in range(n_frames):
            with terminal.frame():
                draw_scene(terminal, cube, floor, frame)
        saved = terminal.transport.save_apng(args.preview, fps=fps or 30.0)  # type: ignore[attr-defined]
        print(f"Wrote {args.preview} ({saved} frames, {args.width}x{args.height})")
        return 0

    if args.dry_run:
        terminal = VectorTerminal(transport=MemoryTransport())
        print("[dry run] no serial port opened")
    else:
        if args.debug:
            print(f"Opening {args.port} at {args.baud} baud (waiting for the device to settle)...")
        terminal = VectorTerminal(port=args.port, baudrate=args.baud)

    reporter = debug.reporter_for(terminal, args.debug)
    drawn = 0
    try:
        while args.frames == 0 or drawn < args.frames:
            with terminal.frame():
                vectors = draw_scene(terminal, cube, floor, drawn)
            if args.dry_run:
                last = terminal.transport.frames[-1]  # type: ignore[attr-defined]
                print(
                    f"frame {drawn:>4}: {len(last):>5} bytes, "
                    f"{vectors} vectors, dist={cube.distance_at(drawn):4.1f}"
                )
            drawn += 1
            if reporter:
                reporter.tick()
            terminal.pace(fps)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        terminal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
