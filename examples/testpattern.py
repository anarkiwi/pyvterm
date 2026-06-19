#!/usr/bin/env python3
"""A low-intensity calibration / link test pattern for pyvterm.

This draws a static, deliberately *simple* pattern so you can tell apart the two
things that go wrong when "nothing draws":

* **Display / calibration** — if the pattern is misshapen, off-centre, clipped
  or wrong brightness, that's the Vectrex side (tune the receiver).
* **The serial link** — if the pattern flickers, comes through partially (e.g.
  just one line), or not at all while the receiver's own splash is rock solid,
  bytes are being lost on the wire. Use ``--vectors`` to scale the pattern's
  complexity: a frame that survives at ``--vectors 4`` but breaks up at
  ``--vectors 200`` is a link/throughput problem, not the display.

The pattern is intentionally dim (``--intensity`` defaults low) so a static
image won't burn the Vectrex phosphor.

Examples
--------
Real hardware (PiTrex/vekterm on a 3.3 V USB-TTL adapter)::

    python examples/testpattern.py --port /dev/ttyUSB0 --baud 2000000

Start simple, then crank up complexity to find where the link breaks::

    python examples/testpattern.py --port /dev/ttyUSB0 --vectors 4
    python examples/testpattern.py --port /dev/ttyUSB0 --vectors 400

No hardware — see the bytes, or render what it should look like::

    python examples/testpattern.py --dry-run
    python examples/testpattern.py --preview pattern.png
"""

from __future__ import annotations

import argparse
import math
import time

from pyvterm import (
    DEFAULT_BAUDRATE,
    DEFAULT_PORT,
    DEFAULT_SYNC_BYTE,
    MemoryTransport,
    VectorTerminal,
)

# The default host bounds are x[-512, 511], y[-384, 383]; stay inside them.
X, Y = 480, 360


def draw_pattern(vt: VectorTerminal, intensity: int, vectors: int) -> None:
    """Draw the test pattern into the terminal's current frame."""
    vt.set_intensity(intensity)

    # Outer border — shows the usable extent (and whether it's clipped).
    vt.polyline([(-X, -Y), (X, -Y), (X, Y), (-X, Y)], closed=True)

    # Crosshair through the origin — shows centring and both axes working.
    vt.move_to(-X, 0).draw_to(X, 0)
    vt.move_to(0, -Y).draw_to(0, Y)

    # Diagonals (an X) — corner reach and linearity.
    vt.move_to(-X, -Y).draw_to(X, Y)
    vt.move_to(-X, Y).draw_to(X, -Y)

    # Small box at the centre — marks (0, 0).
    c = 40
    vt.polyline([(-c, -c), (c, -c), (c, c), (-c, c)], closed=True)

    # Optional extra complexity: a circle approximated by N segments. This is
    # the knob for stressing the link — more segments = more bytes per frame.
    extra = max(0, vectors - 12)
    if extra >= 3:
        r = min(X, Y) * 0.7
        pts = [
            (
                r * math.cos(2 * math.pi * i / extra),
                r * math.sin(2 * math.pi * i / extra),
            )
            for i in range(extra + 1)
        ]
        vt.polyline(pts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Low-intensity Vectrex test pattern over pyvterm.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", default=DEFAULT_PORT, help="serial device path")
    p.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUDRATE,
        help="line rate (match the receiver)",
    )
    p.add_argument(
        "--intensity", type=int, default=4, help="beam brightness 0-15 (keep it low)"
    )
    p.add_argument(
        "--vectors",
        type=int,
        default=12,
        help="approx. number of vectors (link stress knob)",
    )
    p.add_argument("--fps", type=float, default=30.0, help="frames per second")
    p.add_argument("--frames", type=int, default=0, help="frames to send (0 = forever)")
    p.add_argument(
        "--dry-run", action="store_true", help="don't open a port; print frame bytes"
    )
    p.add_argument(
        "--preview", metavar="OUT.png", help="render the pattern to a PNG and exit"
    )
    p.add_argument(
        "--no-flow-control",
        action="store_true",
        help="disable the per-frame handshake and just stream (for a buffered "
        "USB-CDC device; on by default for a raw-UART receiver like vekterm)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.preview:
        from pyvterm.preview import PreviewTransport

        vt = VectorTerminal(transport=PreviewTransport(width=440, height=330))
        with vt.frame():
            draw_pattern(vt, args.intensity, args.vectors)
        vt.transport.save_apng(args.preview, fps=args.fps)  # type: ignore[attr-defined]
        print(f"Wrote {args.preview}")
        return 0

    if args.dry_run:
        vt = VectorTerminal(transport=MemoryTransport())
        with vt.frame():
            draw_pattern(vt, args.intensity, args.vectors)
        data = vt.transport.frames[-1]  # type: ignore[attr-defined]
        print(f"[dry run] frame is {len(data)} bytes ({len(data) // 4} words)")
        return 0

    print(f"Opening {args.port} at {args.baud} baud...")
    flow = None if args.no_flow_control else DEFAULT_SYNC_BYTE
    vt = VectorTerminal(port=args.port, baudrate=args.baud, flow_control=flow)
    period = 1.0 / args.fps if args.fps > 0 else 0.0
    count = 0
    last_report = time.monotonic()
    try:
        while args.frames == 0 or count < args.frames:
            with vt.frame():
                draw_pattern(vt, args.intensity, args.vectors)
            count += 1
            now = time.monotonic()
            if not args.no_flow_control and now - last_report >= 1.0:
                # If 'sent' climbs, the receiver's ready/handshake is working;
                # all-skipped means we never see its sync byte (link/TX issue).
                t = vt.transport
                sent = getattr(t, "frames_sent", "?")
                skipped = getattr(t, "frames_skipped", "?")
                print(f"flow-control: sent={sent} skipped={skipped}")
                last_report = now
            if period:
                time.sleep(period)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        vt.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
