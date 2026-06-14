#!/usr/bin/env python3
"""Rutt-Etra style scan processing of live video on a Vectrex via pyvterm.

The Rutt-Etra Video Synthesizer (1972) displaced a raster's scan lines by
luminance, turning a flat image into a 3D relief built from horizontal lines.
That is exactly what a vector display does well, so this reads video frames
(OpenCV: a file, a camera, or any ``cv2.VideoCapture`` source), reduces each to
a coarse grid, and draws one displaced polyline per scan line.

The Vectrex can only paint so many vectors per refresh before it flickers, so
keep the grid small. Defaults are deliberately modest; turn them down further
with ``--cols`` (horizontal resolution), ``--rows`` (number of scan lines / line
spacing) and ``--fps`` if your display struggles.

Examples
--------
Live webcam (needs OpenCV: ``pip install "pyvterm[video]"``)::

    python examples/ruttetra.py --video 0 --port /dev/ttyACM0

A video file, downscaled and slowed for the Vectrex::

    python examples/ruttetra.py --video clip.mp4 --cols 40 --rows 22 --fps 12 --port /dev/ttyACM0

No camera / off-Linux? Render the built-in synthetic scene to an animated PNG::

    pip install "pyvterm[preview]"
    python examples/ruttetra.py --synthetic --preview ruttetra.png
"""

from __future__ import annotations

import argparse
import contextlib
import math
import time

import numpy as np

from pyvterm import DEFAULT_BAUDRATE, DEFAULT_PORT, MemoryTransport, VectorTerminal

# Screen rectangle the relief is drawn into (host units; bounds are
# X[-512, 511], Y[-384, 383]). Headroom is left at the top for displacement.
X_HALF = 460.0
Y_TOP = 235.0
Y_BOTTOM = -330.0


def _to_gray(frame: np.ndarray) -> np.ndarray:
    """Luminance of a frame; accepts HxW or HxWx3 (BGR, as OpenCV returns)."""
    if frame.ndim == 3:
        # BGR weights (OpenCV channel order): 0.114 B + 0.587 G + 0.299 R.
        return frame[..., 0] * 0.114 + frame[..., 1] * 0.587 + frame[..., 2] * 0.299
    return frame


# --- video sources --------------------------------------------------------


class SyntheticVideoSource:
    """A moving grayscale scene (orbiting blobs + drifting grid), no camera."""

    def __init__(self, width: int = 160, height: int = 120) -> None:
        self.w = width
        self.h = height
        self.t = 0
        yy, xx = np.mgrid[0:height, 0:width]
        self.xx = xx / width
        self.yy = yy / height

    def read(self) -> np.ndarray | None:
        big_t = self.t / 30.0
        self.t += 1
        img = np.zeros((self.h, self.w), dtype=np.float32)
        for radius, speed, phase, size in (
            (0.30, 1.0, 0.0, 0.16),
            (0.24, -0.7, 2.0, 0.11),
            (0.34, 0.5, 4.0, 0.13),
        ):
            cx = 0.5 + radius * math.cos(speed * big_t + phase)
            cy = 0.5 + radius * 0.7 * math.sin(speed * big_t + phase)
            img += np.exp(-((self.xx - cx) ** 2 + (self.yy - cy) ** 2) / (2 * size * size))
        grid = (0.5 + 0.5 * np.sin(2 * np.pi * (3 * self.xx + 0.3 * math.sin(big_t)))) * (
            0.5 + 0.5 * np.sin(2 * np.pi * 2 * self.yy - big_t)
        )
        img += 0.25 * grid
        return np.clip(img, 0.0, 1.0)

    def close(self) -> None:
        pass


class Cv2VideoSource:
    """Read frames from any ``cv2.VideoCapture`` source (file, camera index, URL)."""

    def __init__(
        self,
        source: str,
        target_fps: float | None = None,
        *,
        capture_factory=None,
    ) -> None:
        if capture_factory is None:
            try:
                import cv2
            except ImportError as exc:  # pragma: no cover - platform dependent
                raise SystemExit(
                    "OpenCV is required for --video: pip install 'pyvterm[video]'\n"
                    "(or run with --synthetic)."
                ) from exc
            capture_factory = cv2.VideoCapture
            self._fps_prop = cv2.CAP_PROP_FPS
        else:
            self._fps_prop = 5  # cv2.CAP_PROP_FPS, for injected fakes

        src: object = int(source) if isinstance(source, str) and source.isdigit() else source
        self._cap = capture_factory(src)
        # Sample frames down to the target rate so files play at ~real speed.
        self._skip = 1
        if target_fps and target_fps > 0:
            src_fps = float(self._cap.get(self._fps_prop) or 0.0)
            if src_fps > target_fps:
                self._skip = max(1, round(src_fps / target_fps))

    def read(self) -> np.ndarray | None:
        frame = None
        for _ in range(self._skip):
            ok, frame = self._cap.read()
            if not ok or frame is None:
                return None
        gray = _to_gray(np.asarray(frame, dtype=np.float32))
        return gray / 255.0

    def close(self) -> None:
        with contextlib.suppress(Exception):  # pragma: no cover - cleanup best effort
            self._cap.release()


# --- Rutt-Etra scan processor --------------------------------------------


class RuttEtra:
    """Reduce frames to a grid and project them as luminance-displaced scan lines."""

    def __init__(
        self,
        cols: int,
        rows: int,
        *,
        displacement: float = 95.0,
        depth: float = 0.18,
        gamma: float = 1.0,
        invert: bool = False,
        threshold: float = 0.0,
        intensity: int = 13,
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.displacement = displacement
        self.depth = depth
        self.gamma = gamma
        self.invert = invert
        self.threshold = threshold
        self.intensity = intensity

    def _reduce(self, frame: np.ndarray) -> np.ndarray:
        """Nearest-neighbour downsample to (rows, cols), normalised to [0, 1]."""
        h, w = frame.shape
        ys = np.linspace(0, h - 1, self.rows).astype(int)
        xs = np.linspace(0, w - 1, self.cols).astype(int)
        small = frame[ys][:, xs].astype(np.float32)

        lo, hi = float(small.min()), float(small.max())
        small = (small - lo) / (hi - lo) if hi - lo > 1e-6 else np.zeros_like(small)
        if self.invert:
            small = 1.0 - small
        if self.gamma != 1.0:
            small = small**self.gamma
        return small

    def scanlines(self, frame: np.ndarray) -> list[list[tuple[float, float]]]:
        """Project a frame into a list of polylines (one or more per scan line).

        A scan line is split into separate polylines wherever the luminance
        drops below ``threshold``, so dark areas leave gaps (and fewer vectors).
        """
        small = self._reduce(frame)
        runs: list[list[tuple[float, float]]] = []
        last_col = self.cols - 1 or 1
        last_row = self.rows - 1 or 1
        for r in range(self.rows):
            frac = r / last_row  # 0 = top of image (back), 1 = bottom (front)
            base_y = Y_TOP + frac * (Y_BOTTOM - Y_TOP)
            scale = 1.0 - self.depth * (1.0 - frac)  # rows recede/narrow toward the back
            run: list[tuple[float, float]] = []
            for c in range(self.cols):
                lum = float(small[r, c])
                if lum < self.threshold:
                    if len(run) >= 2:
                        runs.append(run)
                    run = []
                    continue
                x = (c / last_col - 0.5) * (2 * X_HALF) * scale
                y = base_y + lum * self.displacement * scale
                run.append((x, y))
            if len(run) >= 2:
                runs.append(run)
        return runs

    def draw(self, terminal: VectorTerminal, frame: np.ndarray) -> int:
        """Draw a frame's scan lines into the terminal; returns vectors emitted."""
        runs = self.scanlines(frame)
        terminal.set_intensity(self.intensity)
        vectors = 0
        for run in runs:
            terminal.polyline(run)
            vectors += len(run) - 1
        return vectors


# --- CLI ------------------------------------------------------------------


def make_source(args: argparse.Namespace) -> SyntheticVideoSource | Cv2VideoSource:
    if args.video is not None:
        return Cv2VideoSource(args.video, target_fps=args.fps)
    if not args.synthetic and not args.preview:
        print(
            "No --video given; using the synthetic scene (pass --video FILE|INDEX for real video)."
        )
    return SyntheticVideoSource()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rutt-Etra style video scan processing on a Vectrex via pyvterm.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = parser.add_argument_group("video input")
    src.add_argument("--video", help="OpenCV source: a file path, URL, or camera index (e.g. 0)")
    src.add_argument("--synthetic", action="store_true", help="use the built-in moving scene")

    grid = parser.add_argument_group("scan processing (turn down if the display flickers)")
    grid.add_argument("--cols", type=int, default=44, help="samples per scan line (horizontal res)")
    grid.add_argument("--rows", type=int, default=24, help="scan lines (fewer = wider spacing)")
    grid.add_argument(
        "--displacement", type=float, default=95.0, help="luminance height (host units)"
    )
    grid.add_argument("--depth", type=float, default=0.18, help="perspective narrowing, 0=face-on")
    grid.add_argument(
        "--gamma", type=float, default=1.0, help="contrast gamma applied to luminance"
    )
    grid.add_argument(
        "--threshold", type=float, default=0.0, help="blank scan line below this [0,1]"
    )
    grid.add_argument("--invert", action="store_true", help="invert luminance")
    grid.add_argument("--intensity", type=int, default=13, help="beam brightness 0-15")

    out = parser.add_argument_group("output")
    out.add_argument("--port", default=DEFAULT_PORT, help="serial device path")
    out.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help="nominal baud rate")
    out.add_argument("--fps", type=float, default=18.0, help="target frames per second")
    out.add_argument(
        "--frames", type=int, default=0, help="frames to run (0 = until end / forever)"
    )
    out.add_argument("--dry-run", action="store_true", help="don't open a serial port")
    out.add_argument("--preview", metavar="OUT.png", help="render an animated PNG and exit")
    out.add_argument("--width", type=int, default=440, help="preview width (px)")
    out.add_argument("--height", type=int, default=330, help="preview height (px)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = make_source(args)
    processor = RuttEtra(
        args.cols,
        args.rows,
        displacement=args.displacement,
        depth=args.depth,
        gamma=args.gamma,
        invert=args.invert,
        threshold=args.threshold,
        intensity=args.intensity,
    )
    if args.cols * args.rows > 2500:
        print(
            f"Note: {args.cols}x{args.rows} = {args.cols * args.rows} samples may flicker; "
            "reduce --cols/--rows/--fps if so."
        )

    if args.preview:
        from pyvterm.preview import PreviewTransport

        terminal = VectorTerminal(transport=PreviewTransport(width=args.width, height=args.height))
        limit = args.frames or 90
        print(f"Rendering up to {limit} frames to {args.preview} ...")
        for _ in range(limit):
            frame = source.read()
            if frame is None:
                break
            with terminal.frame():
                processor.draw(terminal, frame)
        saved = terminal.transport.save_apng(args.preview, fps=args.fps)  # type: ignore[attr-defined]
        source.close()
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
            frame = source.read()
            if frame is None:
                print("End of video.")
                break
            with terminal.frame():
                vectors = processor.draw(terminal, frame)
            if args.dry_run:
                last = terminal.transport.frames[-1]  # type: ignore[attr-defined]
                print(f"frame {drawn:>4}: {len(last):>5} bytes, {vectors} vectors")
            drawn += 1
            if period:
                time.sleep(period)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        terminal.close()
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
