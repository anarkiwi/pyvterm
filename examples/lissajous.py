#!/usr/bin/env python3
"""Animated Lissajous curves on a Vectrex via pyvterm.

A Lissajous curve is the path of a point whose X and Y both follow sine waves::

    x = A * sin(a * t + delta)
    y = B * sin(b * t)

Varying the phase ``delta`` over time makes the figure morph and rotate, which
looks great on a vector display. This script builds one polyline per frame and
streams it to the device.

Examples
--------
Run on real hardware (USB-DVG / PiTrex on /dev/ttyUSB0)::

    python examples/lissajous.py --port /dev/ttyUSB0

Try it without any hardware (prints per-frame byte counts and exits)::

    python examples/lissajous.py --dry-run --frames 5
"""

from __future__ import annotations

import argparse
import math

from pyvterm import DEFAULT_BAUDRATE, DEFAULT_PORT, MemoryTransport, VectorTerminal


def lissajous_points(
    a: float,
    b: float,
    delta: float,
    samples: int,
    amp_x: float,
    amp_y: float,
) -> list[tuple[float, float]]:
    """Return ``samples + 1`` points tracing one full Lissajous figure."""
    points = []
    for i in range(samples + 1):
        t = 2.0 * math.pi * i / samples
        x = amp_x * math.sin(a * t + delta)
        y = amp_y * math.sin(b * t)
        points.append((x, y))
    return points


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draw animated Lissajous patterns on a Vectrex via pyvterm.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help="serial device path")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help="nominal baud rate")
    parser.add_argument("-a", type=float, default=3.0, help="X frequency ratio")
    parser.add_argument("-b", type=float, default=2.0, help="Y frequency ratio")
    parser.add_argument("--samples", type=int, default=400, help="points per figure")
    parser.add_argument("--intensity", type=int, default=15, help="beam brightness 0-15")
    parser.add_argument(
        "--speed", type=float, default=0.05, help="phase increment (radians) per frame"
    )
    parser.add_argument(
        "--fps",
        default="auto",
        help="target frames per second, or 'auto' (default) to let the device pace the stream",
    )
    parser.add_argument(
        "--frames", type=int, default=0, help="number of frames to draw (0 = run forever)"
    )
    parser.add_argument("--amp-x", type=float, default=480.0, help="X amplitude")
    parser.add_argument("--amp-y", type=float, default=360.0, help="Y amplitude")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't open a serial port; print frame sizes instead",
    )
    parser.add_argument("--preview", metavar="OUT.png", help="render an animated PNG and exit")
    parser.add_argument("--width", type=int, default=440, help="preview width (px)")
    parser.add_argument("--height", type=int, default=330, help="preview height (px)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # fps "auto" -> None (the device paces the stream); else a numeric target.
    fps = None if str(args.fps).lower() == "auto" else float(args.fps)

    if args.preview:
        from pyvterm.preview import PreviewTransport

        n_frames = args.frames or 120
        terminal = VectorTerminal(transport=PreviewTransport(width=args.width, height=args.height))
        print(f"Rendering {n_frames} frames to {args.preview} ...")
        for i in range(n_frames):
            # Sweep the phase through a full turn so the loop is seamless.
            delta = 2.0 * math.pi * i / n_frames
            points = lissajous_points(args.a, args.b, delta, args.samples, args.amp_x, args.amp_y)
            with terminal.frame():
                terminal.set_intensity(args.intensity)
                terminal.polyline(points)
        saved = terminal.transport.save_apng(args.preview, fps=fps or 30.0)  # type: ignore[attr-defined]
        print(f"Wrote {args.preview} ({saved} frames, {args.width}x{args.height})")
        return 0

    if args.dry_run:
        terminal = VectorTerminal(transport=MemoryTransport())
        print("[dry run] no serial port opened; printing frame sizes")
    else:
        print(f"Opening {args.port} at {args.baud} baud (waiting for the device to settle)...")
        terminal = VectorTerminal(port=args.port, baudrate=args.baud)

    delta = 0.0
    drawn = 0
    try:
        while args.frames == 0 or drawn < args.frames:
            points = lissajous_points(args.a, args.b, delta, args.samples, args.amp_x, args.amp_y)
            with terminal.frame():
                terminal.set_intensity(args.intensity)
                terminal.polyline(points)

            if args.dry_run:
                last = terminal.transport.frames[-1]  # type: ignore[attr-defined]
                print(
                    f"frame {drawn:>4}: {len(last):>5} bytes, "
                    f"{len(points)} points, delta={delta:5.2f}"
                )

            delta += args.speed
            drawn += 1
            terminal.pace(fps)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        terminal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
