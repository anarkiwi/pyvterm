#!/usr/bin/env python3
"""3D waterfall spectrum analyzer on a Vectrex via pyvterm.

Captures live audio, runs an FFT every frame, and draws a scrolling 3D
waterfall on the vector display: **frequency** across X, **magnitude** as
height, and **time** receding into the distance. The newest spectrum is the
bright trace at the front; older spectra shrink and rise into the back.

Audio input
-----------
* ``--device <alsa-device>`` captures from ALSA via `pyalsaaudio` (Linux). To
  visualise what's playing, point it at a *monitor* of the default output.
  List capture devices with ``arecord -L``; with PulseAudio/PipeWire the
  ``pulse`` device plus ``PULSE_SOURCE=<sink>.monitor`` taps the output, e.g.::

      pip install "pyvterm[analyzer]" pyalsaaudio
      PULSE_SOURCE=@DEFAULT_SINK@.monitor python examples/spectrum3d.py --device pulse

* ``--synthetic`` uses a built-in signal generator instead — handy off-Linux
  or with no sound card.

Output
------
* ``--port /dev/ttyACM0`` streams to real hardware (the default).
* ``--dry-run`` builds frames without opening a serial port.
* ``--preview out.png`` renders an **animated PNG** simulating the glowing
  vector display (no hardware needed), then exits::

      pip install "pyvterm[preview]"
      python examples/spectrum3d.py --synthetic --preview spectrum3d.png --frames 90
"""

from __future__ import annotations

import argparse
import math
from collections import deque
from collections.abc import Iterator

import numpy as np

from pyvterm import (
    DEFAULT_BAUDRATE,
    DEFAULT_BOUNDS,
    DEFAULT_PORT,
    Bounds,
    MemoryTransport,
    VectorTerminal,
)

# --- defaults -------------------------------------------------------------

DEFAULT_SAMPLE_RATE = 44_100
DEFAULT_FRAME_SIZE = 1024
DEFAULT_BINS = 32
DEFAULT_HISTORY = 16
DEFAULT_FMAX = 8_000.0


# --- audio sources --------------------------------------------------------


class SyntheticSource:
    """Generate audio with a lively, moving spectrum (no hardware needed)."""

    def __init__(self, sample_rate: int, frame_size: int, seed: int = 1) -> None:
        self.sr = sample_rate
        self.n = frame_size
        self.pos = 0
        self._rng = np.random.default_rng(seed)

    def read_frame(self) -> np.ndarray:
        n, sr = self.n, self.sr
        t = (self.pos + np.arange(n)) / sr
        big_t = self.pos / sr  # slow clock for drifting parameters
        self.pos += n

        beat = 0.5 * (1.0 + np.sin(2 * np.pi * 2.0 * t))  # 2 Hz pulse
        sig = np.zeros(n, dtype=np.float64)
        # Bass kick, gated by the beat.
        sig += 0.7 * np.sin(2 * np.pi * 70.0 * t) * np.clip(beat, 0.0, 1.0) ** 3
        # Two mid partials whose pitch drifts so peaks sweep across the screen.
        f1 = 350.0 + 220.0 * math.sin(2 * np.pi * 0.13 * big_t)
        f2 = 900.0 + 500.0 * math.sin(2 * np.pi * 0.10 * big_t + 1.0)
        sig += 0.5 * np.sin(2 * np.pi * f1 * t)
        sig += 0.35 * np.sin(2 * np.pi * f2 * t)
        # High shimmer, fading in and out.
        f3 = 3200.0 + 1500.0 * math.sin(2 * np.pi * 0.18 * big_t)
        shimmer = 0.5 * (1.0 + math.sin(2 * np.pi * 0.40 * big_t))
        sig += 0.18 * shimmer * np.sin(2 * np.pi * f3 * t)
        # A little noise floor.
        sig += 0.02 * self._rng.standard_normal(n)
        return sig.astype(np.float32)


class AlsaSource:
    """Capture mono audio from an ALSA device via `pyalsaaudio` (Linux only)."""

    def __init__(self, device: str, sample_rate: int, frame_size: int, channels: int = 1) -> None:
        try:
            import alsaaudio
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise SystemExit(
                "pyalsaaudio is required for live ALSA capture: pip install pyalsaaudio\n"
                "(or run with --synthetic). It is Linux-only."
            ) from exc

        self.n = frame_size
        self.channels = channels
        self._buf = np.zeros(0, dtype=np.float32)
        self._pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE,
            mode=alsaaudio.PCM_NORMAL,
            device=device,
            channels=channels,
            rate=sample_rate,
            format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=frame_size,
        )

    def read_frame(self) -> np.ndarray:
        while len(self._buf) < self.n:
            length, data = self._pcm.read()
            if length <= 0 or not data:
                continue
            arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            if self.channels > 1:
                arr = arr.reshape(-1, self.channels).mean(axis=1)
            self._buf = np.concatenate([self._buf, arr])
        frame, self._buf = self._buf[: self.n], self._buf[self.n :]
        return frame


# --- analysis -------------------------------------------------------------


class Analyzer:
    """Turn a block of samples into ``n_bins`` normalised magnitudes in [0, 1].

    Uses a Hann-windowed real FFT, log-spaced frequency bins, a log magnitude
    scale, decaying auto-gain, and per-bin attack/release smoothing so the
    display moves nicely.
    """

    def __init__(
        self,
        sample_rate: int,
        frame_size: int,
        n_bins: int = DEFAULT_BINS,
        f_min: float = 40.0,
        f_max: float = DEFAULT_FMAX,
    ) -> None:
        self.window = np.hanning(frame_size).astype(np.float32)
        freqs = np.fft.rfftfreq(frame_size, 1.0 / sample_rate)
        edges = np.geomspace(f_min, min(f_max, sample_rate / 2), n_bins + 1)
        self._bins = [
            np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(n_bins)
        ]
        self.n_bins = n_bins
        self._level = np.zeros(n_bins, dtype=np.float32)
        self._peak = 1e-6

    def process(self, samples: np.ndarray) -> np.ndarray:
        spectrum = np.abs(np.fft.rfft(samples * self.window))
        mags = np.array(
            [float(spectrum[idx].mean()) if idx.size else 0.0 for idx in self._bins],
            dtype=np.float32,
        )
        mags = np.log10(1.0 + mags)
        # Decaying auto-gain so quiet and loud passages both fill the display.
        self._peak = max(self._peak * 0.995, float(mags.max()), 1e-6)
        norm = np.clip(mags / self._peak, 0.0, 1.0)
        # Fast attack, slow release.
        rising = norm > self._level
        self._level += (norm - self._level) * np.where(rising, 0.6, 0.18)
        return self._level.copy()


# --- 3D waterfall projection ---------------------------------------------


class Waterfall3D:
    """Hold a history of spectra and project them as receding 3D traces."""

    def __init__(
        self,
        n_bins: int = DEFAULT_BINS,
        history: int = DEFAULT_HISTORY,
        bounds: Bounds = DEFAULT_BOUNDS,
        row_width: float = 820.0,
        front_y: float = -300.0,
        depth_rise: float = 36.0,
        depth_shrink: float = 0.45,
        depth_skew: float = 6.0,
        mag_height: float = 150.0,
    ) -> None:
        self.n_bins = n_bins
        self.history_len = history
        self.bounds = bounds
        self.row_width = row_width
        self.front_y = front_y
        self.depth_rise = depth_rise
        self.depth_shrink = depth_shrink
        self.depth_skew = depth_skew
        self.mag_height = mag_height
        self._history: deque[np.ndarray] = deque(maxlen=history)

    def push(self, levels: np.ndarray) -> None:
        self._history.appendleft(np.asarray(levels, dtype=np.float32))

    def rows(self) -> Iterator[tuple[int, list[tuple[float, float]]]]:
        """Yield ``(intensity, points)`` for each stored spectrum, front first."""
        m = self.history_len
        mid = (m - 1) / 2.0
        for r, levels in enumerate(self._history):
            d = r / (m - 1) if m > 1 else 0.0  # 0 = front (newest), 1 = back
            scale = 1.0 - self.depth_shrink * d
            base_x = -self.row_width * scale / 2.0 + self.depth_skew * (r - mid)
            base_y = self.front_y + r * self.depth_rise
            points = [
                (
                    base_x + (i / (self.n_bins - 1)) * self.row_width * scale,
                    base_y + float(levels[i]) * self.mag_height * scale,
                )
                for i in range(self.n_bins)
            ]
            intensity = int(round(15.0 - 11.0 * d))  # front bright, back dim
            yield max(intensity, 3), points

    def draw(self, terminal: VectorTerminal) -> None:
        """Render all traces into the terminal's current frame (back to front)."""
        for intensity, points in reversed(list(self.rows())):
            terminal.set_intensity(intensity)
            terminal.polyline(points)


# --- animated-PNG preview -------------------------------------------------


def render_preview(
    path: str,
    source: SyntheticSource | AlsaSource,
    analyzer: Analyzer,
    waterfall: Waterfall3D,
    frames: int,
    fps: float,
    width: int,
    height: int,
) -> None:
    """Capture ``frames`` frames and save them as an animated PNG."""
    from pyvterm.preview import PreviewTransport

    terminal = VectorTerminal(transport=PreviewTransport(width=width, height=height))
    print(f"Rendering {frames} frames to {path} ...")
    for _ in range(frames):
        waterfall.push(analyzer.process(source.read_frame()))
        with terminal.frame():
            waterfall.draw(terminal)
    saved = terminal.transport.save_apng(path, fps=fps)  # type: ignore[attr-defined]
    print(f"Wrote {path} ({saved} frames, {width}x{height})")


# --- CLI ------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="3D waterfall spectrum analyzer on a Vectrex via pyvterm.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = parser.add_argument_group("audio input")
    src.add_argument("--device", default=None, help="ALSA capture device (e.g. pulse, default)")
    src.add_argument("--synthetic", action="store_true", help="use the built-in signal generator")
    src.add_argument("--rate", type=int, default=DEFAULT_SAMPLE_RATE, help="sample rate (Hz)")
    src.add_argument("--frame-size", type=int, default=DEFAULT_FRAME_SIZE, help="samples per FFT")
    src.add_argument("--fmax", type=float, default=DEFAULT_FMAX, help="top of frequency axis (Hz)")

    disp = parser.add_argument_group("display")
    disp.add_argument("--bins", type=int, default=DEFAULT_BINS, help="frequency bins (X axis)")
    disp.add_argument("--history", type=int, default=DEFAULT_HISTORY, help="waterfall depth (rows)")
    disp.add_argument("--fps", type=float, default=25.0, help="target frames per second")

    out = parser.add_argument_group("output")
    out.add_argument("--port", default=DEFAULT_PORT, help="serial device path")
    out.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE, help="nominal baud rate")
    out.add_argument("--dry-run", action="store_true", help="don't open a serial port")
    out.add_argument("--frames", type=int, default=0, help="frames to run (0 = forever)")
    out.add_argument("--preview", metavar="OUT.png", help="render an animated PNG and exit")
    out.add_argument("--width", type=int, default=480, help="preview width (px)")
    out.add_argument("--height", type=int, default=360, help="preview height (px)")
    return parser.parse_args(argv)


def make_source(args: argparse.Namespace) -> SyntheticSource | AlsaSource:
    if args.synthetic or args.device is None:
        if not args.synthetic and args.device is None and not args.preview:
            print("No --device given; using the synthetic source (pass --device for live ALSA).")
        return SyntheticSource(args.rate, args.frame_size)
    return AlsaSource(args.device, args.rate, args.frame_size)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = make_source(args)
    analyzer = Analyzer(args.rate, args.frame_size, n_bins=args.bins, f_max=args.fmax)
    waterfall = Waterfall3D(n_bins=args.bins, history=args.history)

    if args.preview:
        render_preview(
            args.preview,
            source,
            analyzer,
            waterfall,
            frames=args.frames or 90,
            fps=args.fps,
            width=args.width,
            height=args.height,
        )
        return 0

    if args.dry_run:
        terminal = VectorTerminal(transport=MemoryTransport())
        print("[dry run] no serial port opened")
    else:
        print(f"Opening {args.port} at {args.baud} baud (waiting for the device to settle)...")
        terminal = VectorTerminal(port=args.port, baudrate=args.baud)

    import time

    period = 1.0 / args.fps if args.fps > 0 else 0.0
    drawn = 0
    try:
        while args.frames == 0 or drawn < args.frames:
            waterfall.push(analyzer.process(source.read_frame()))
            with terminal.frame():
                waterfall.draw(terminal)
            if args.dry_run:
                last = terminal.transport.frames[-1]  # type: ignore[attr-defined]
                print(f"frame {drawn:>4}: {len(last):>5} bytes, {waterfall.n_bins} bins")
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
