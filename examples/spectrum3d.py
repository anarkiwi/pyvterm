#!/usr/bin/env python3
"""3D waterfall spectrum analyzer on a Vectrex via pyvterm.

Captures live audio, runs an FFT every frame, and draws a scrolling 3D
waterfall on the vector display: **frequency** across X, **magnitude** as
height, and **time** receding into the distance. The newest spectrum is the
bright trace at the front; older spectra shrink and rise into the back.

The whole slab is viewed through an **orbiting perspective camera** rather than
a fixed projection, and the camera is alive:

* In steady state it **drifts slowly** — a gentle, continuous sway of the
  viewing angle, so the perspective is never quite still.
* When the analyzer detects a **major change** in the audio (an onset / beat /
  new texture, via spectral flux) the camera **swings to a fresh viewpoint**,
  easing smoothly into a new angle that re-frames the spectrum.

Pass ``--no-rotate`` to hold a fixed head-on view instead.

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
* ``--port /dev/ttyUSB0`` streams to real hardware (the default).
* ``--dry-run`` builds frames without opening a serial port.
* ``--preview out.png`` renders an **animated PNG** simulating the glowing
  vector display (no hardware needed), then exits::

      pip install "pyvterm[preview]"
      python examples/spectrum3d.py --synthetic --preview spectrum3d.png --frames 120
"""

from __future__ import annotations

import argparse
import math
from collections import deque
from collections.abc import Iterator

import numpy as np

from pyvterm import (
    DEFAULT_BOUNDS,
    DEFAULT_PORT,
    DEFAULT_SYNC_BYTE,
    Bounds,
    MemoryTransport,
    VectorTerminal,
    debug,
)

# --- defaults -------------------------------------------------------------

DEFAULT_SAMPLE_RATE = 44_100
DEFAULT_FRAME_SIZE = 1024
DEFAULT_BINS = 32
DEFAULT_HISTORY = 16
DEFAULT_FMAX = 8_000.0

# Change detection (how readily the camera jumps to a new perspective).
DEFAULT_SENSITIVITY = 2.4  # flux must exceed this multiple of its recent average
DEFAULT_COOLDOWN = 16  # frames to wait before another jump can fire

# Camera framing. The waterfall sits centred on the origin in model space; the
# camera looks at it from `CAM_DISTANCE` away with focal length CAM_SIZE*distance.
# Scale and yaw are kept moderate so even a strong swing stays inside the screen.
CAM_DISTANCE = 4.8  # camera distance in model units (larger = flatter perspective)
CAM_SIZE = 300.0  # host units per model unit at the base distance

#: Preset base viewpoints ``(yaw, pitch)`` in radians, cycled on each major
#: change. They alternate left/right and vary the tilt so successive jumps land
#: on visibly different angles. The first is the classic head-on view.
VIEW_PRESETS: list[tuple[float, float]] = [
    (0.00, 0.52),  # head-on, classic waterfall
    (0.40, 0.46),  # swung to the right, a touch flatter
    (-0.38, 0.50),  # swung to the left
    (0.20, 0.64),  # right, steeper / more top-down
    (-0.26, 0.44),  # left, lower and flatter
    (0.44, 0.54),  # strong right
    (-0.42, 0.58),  # strong left, steep
]


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
    """Capture mono audio from an ALSA device via `pyalsaaudio` (Linux only).

    Always returns the **latest** ``frame_size`` samples and throws the backlog
    away. A spectrum display only ever wants "now": the draw loop (FFT + the
    serial flow-control handshake + the Vectrex redraw) runs slower than audio
    arrives — perhaps ~20 fps against ~47 audio periods/sec — so a blocking,
    one-period-at-a-time read would let the capture buffer grow without bound and
    the display would fall further and further behind real time. The classic
    symptom is exactly that lag: barely a flicker while music plays, then a
    burst of "crazy" stale audio the moment it stops and the buffer drains. So we
    read non-blocking, drain every queued period each call, and keep only the
    newest frame — the display stays live no matter how slow the draw loop is.
    """

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
        self._alsa = alsaaudio
        self._buf = np.zeros(0, dtype=np.float32)
        self._pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE,
            mode=alsaaudio.PCM_NONBLOCK,
            device=device,
            channels=channels,
            rate=sample_rate,
            format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=frame_size,
        )

    def _drain(self) -> None:
        """Pull every period currently queued into ``self._buf`` (non-blocking)."""
        while True:
            try:
                length, data = self._pcm.read()
            except self._alsa.ALSAAudioError:
                break  # overrun/xrun: the PCM recovers on the next call
            if length <= 0 or not data:
                break  # nothing more available right now
            arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            if self.channels > 1:
                arr = arr.reshape(-1, self.channels).mean(axis=1)
            self._buf = np.concatenate([self._buf, arr])

    def read_frame(self) -> np.ndarray:
        import time

        # Drain the whole backlog. Only spin (briefly) if nothing is buffered yet
        # — startup, or the rare case the draw loop outruns the audio.
        for _ in range(200):
            self._drain()
            if len(self._buf) >= self.n:
                break
            time.sleep(0.001)
        if len(self._buf) < self.n:  # never filled (shouldn't happen): pad
            return np.zeros(self.n, dtype=np.float32)
        frame = self._buf[-self.n :].copy()  # the newest frame; drop the rest
        self._buf = np.zeros(0, dtype=np.float32)
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
        tilt_db_per_oct: float = 3.0,
    ) -> None:
        self.window = np.hanning(frame_size).astype(np.float32)
        freqs = np.fft.rfftfreq(frame_size, 1.0 / sample_rate)
        edges = np.geomspace(f_min, min(f_max, sample_rate / 2), n_bins + 1)
        self._bins = [
            np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(n_bins)
        ]
        # Spectral tilt ("pink-noise" pre-emphasis). Music and most real signals
        # fall ~3 dB/octave toward higher frequencies, so the low end carries far
        # more energy and dominates any global normalisation — the bass bins peg
        # full-height and everything above them reads as flat. Pre-emphasising by
        # +tilt_db_per_oct dB/octave flattens that natural slope so every band
        # gets a fair share of the display. Centres are the geometric mean of
        # each bin's edges; weight ∝ (f / f0) ** (tilt_db / 6.02 per octave).
        centers = np.sqrt(edges[:-1] * edges[1:])
        octaves = np.log2(centers / centers[0])
        self._tilt = (10.0 ** (tilt_db_per_oct * octaves / 20.0)).astype(np.float32)
        self.n_bins = n_bins
        self._level = np.zeros(n_bins, dtype=np.float32)
        self._peak = 1e-6

    def process(self, samples: np.ndarray) -> np.ndarray:
        spectrum = np.abs(np.fft.rfft(samples * self.window))
        mags = np.array(
            [float(spectrum[idx].mean()) if idx.size else 0.0 for idx in self._bins],
            dtype=np.float32,
        )
        mags = mags * self._tilt  # flatten the bass-heavy spectral slope
        mags = np.log10(1.0 + mags)
        # Decaying auto-gain so quiet and loud passages both fill the display.
        # Normalise to a high *percentile*, not the single loudest bin: one
        # dominant peak (still, usually, the low end) then can't set the gain for
        # the whole display and crush every other band to zero.
        loud = float(np.percentile(mags, 90)) if self.n_bins >= 4 else float(mags.max())
        self._peak = max(self._peak * 0.995, loud, 1e-6)
        norm = np.clip(mags / self._peak, 0.0, 1.0)
        # Fast attack, slow release.
        rising = norm > self._level
        self._level += (norm - self._level) * np.where(rising, 0.6, 0.18)
        return self._level.copy()


class ChangeDetector:
    """Flag a *major change* in the spectrum (an onset, beat, or new texture).

    Uses **spectral flux** — the summed rise in per-bin level since the last
    frame — compared against an adaptive baseline. When the flux spikes well
    above its recent average (``sensitivity`` times it) and a refractory
    ``cooldown`` has elapsed since the last hit, :meth:`update` returns ``True``
    once. Because the baseline adapts, it fires on changes at any volume rather
    than only during loud passages.
    """

    def __init__(
        self,
        sensitivity: float = DEFAULT_SENSITIVITY,
        cooldown: int = DEFAULT_COOLDOWN,
        floor: float = 0.015,
        adapt: float = 0.08,
        warmup: int = 6,
    ) -> None:
        self.sensitivity = sensitivity
        self.cooldown = cooldown
        self.floor = floor  # ignore flux below this (silence / noise jitter)
        self.adapt = adapt  # EMA rate for the adaptive baseline
        self._prev: np.ndarray | None = None
        self._avg = 0.0
        self._cool = 0
        self._warm = warmup
        self.flux = 0.0  # last computed flux (exposed for display/debug)

    def update(self, levels: np.ndarray) -> bool:
        """Feed one spectrum; return ``True`` exactly when a major change fires."""
        levels = np.asarray(levels, dtype=np.float32)
        if self._prev is None:
            self._prev = levels.copy()
            return False
        flux = float(np.maximum(levels - self._prev, 0.0).sum()) / max(1, levels.size)
        self._prev = levels.copy()
        self.flux = flux

        triggered = False
        if self._warm > 0:
            self._warm -= 1  # let the baseline settle before firing
        elif self._cool > 0:
            self._cool -= 1
        elif flux > self.floor and flux > self._avg * self.sensitivity:
            triggered = True
            self._cool = self.cooldown
        # Adapt the baseline toward the current flux (after the test, so a spike
        # doesn't immediately mask itself).
        self._avg += (flux - self._avg) * self.adapt
        return triggered


# --- orbiting camera ------------------------------------------------------


class Camera:
    """An orbiting perspective camera that eases between viewpoints.

    The waterfall is centred on the origin in model space (frequency along X,
    magnitude up Y, time along Z). The camera looks at it from ``distance``
    away and can swing around it: ``yaw`` orbits left/right and ``pitch`` tips
    the view between edge-on and top-down. Both ease toward a target every
    frame, so motion glides rather than snaps.

    Two things drive it:

    * a slow, continuous **sway** around the current base view — the
      steady-state drift, so the perspective is never perfectly still;
    * a **jump** to the next preset view, called when a major change is
      detected, which the easing turns into a sweeping move to a fresh angle.
    """

    def __init__(
        self,
        *,
        distance: float = CAM_DISTANCE,
        size: float = CAM_SIZE,
        ease: float = 0.1,
        sway_yaw: float = 0.14,
        sway_pitch: float = 0.06,
        sway_period: float = 480.0,
        presets: list[tuple[float, float]] = VIEW_PRESETS,
    ) -> None:
        self.distance = distance
        self.focal = size * distance
        self.ease = ease
        self.sway_yaw = sway_yaw
        self.sway_pitch = sway_pitch
        self.sway_rate = 2.0 * math.pi / max(1.0, sway_period)
        self.presets = list(presets)
        self._view = 0
        self.base_yaw, self.base_pitch = self.presets[0]
        # Start already settled on the first view.
        self.yaw = self.target_yaw = self.base_yaw
        self.pitch = self.target_pitch = self.base_pitch
        self._phase = 0.0

    def jump(self) -> None:
        """Advance to the next preset viewpoint (called on a major change)."""
        self._view = (self._view + 1) % len(self.presets)
        self.base_yaw, self.base_pitch = self.presets[self._view]

    def update(self) -> None:
        """Advance the slow sway, then ease the live angles toward their target."""
        self._phase += self.sway_rate
        self.target_yaw = self.base_yaw + self.sway_yaw * math.sin(self._phase)
        self.target_pitch = self.base_pitch + self.sway_pitch * math.sin(0.73 * self._phase + 1.3)
        self.yaw += (self.target_yaw - self.yaw) * self.ease
        self.pitch += (self.target_pitch - self.pitch) * self.ease

    def project(self, x: float, y: float, z: float) -> tuple[float, float]:
        """Project a model-space point to host screen coordinates.

        Rotates about the vertical axis (``yaw``) then tips by ``pitch`` so we
        look down on the slab, pushes it ``distance`` away from the camera, and
        applies a perspective divide.
        """
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        x1 = x * cy + z * sy
        z1 = -x * sy + z * cy
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        y2 = y * cp + z1 * sp  # positive pitch lifts the far edge up the screen
        z2 = -y * sp + z1 * cp
        zc = z2 + self.distance
        if zc < 0.1:  # keep points in front of the camera
            zc = 0.1
        return (self.focal * x1 / zc, self.focal * y2 / zc)


# --- 3D waterfall projection ---------------------------------------------


class Waterfall3D:
    """Hold a history of spectra and project them as a 3D slab through a camera.

    Each stored spectrum is a row of points in model space — frequency along X,
    magnitude as height (Y), and age along Z (newest near the camera, oldest
    receding into the distance). A :class:`Camera` turns those 3D points into
    screen coordinates, so the same slab can be viewed from any angle.
    """

    def __init__(
        self,
        n_bins: int = DEFAULT_BINS,
        history: int = DEFAULT_HISTORY,
        bounds: Bounds = DEFAULT_BOUNDS,
        width_span: float = 1.9,
        depth_span: float = 2.0,
        mag_span: float = 0.85,
        lift: float = 0.28,
    ) -> None:
        self.n_bins = n_bins
        self.history_len = history
        self.bounds = bounds
        self.width_span = width_span  # model width of the frequency axis
        self.depth_span = depth_span  # model depth from newest to oldest row
        self.mag_span = mag_span  # model height of a full-scale magnitude
        self.lift = lift  # drop the slab so it sits centred on screen
        self._history: deque[np.ndarray] = deque(maxlen=history)
        self._camera = Camera()  # default view for rows()/draw() without one

    def push(self, levels: np.ndarray) -> None:
        self._history.appendleft(np.asarray(levels, dtype=np.float32))

    def _model_point(self, i: int, depth: float, level: float) -> tuple[float, float, float]:
        """Model-space ``(x, y, z)`` for bin ``i`` of a row at ``depth`` in [0, 1]."""
        fx = (i / (self.n_bins - 1) - 0.5) if self.n_bins > 1 else 0.0
        x = fx * self.width_span
        z = (depth - 0.5) * self.depth_span  # newest (depth 0) nearest the camera
        y = level * self.mag_span - self.lift
        return x, y, z

    def rows(self, camera: Camera | None = None) -> Iterator[tuple[int, list[tuple[float, float]]]]:
        """Yield ``(intensity, points)`` for each stored spectrum, front first."""
        cam = camera or self._camera
        m = self.history_len
        for r, levels in enumerate(self._history):
            d = r / (m - 1) if m > 1 else 0.0  # 0 = front (newest), 1 = back
            points = [
                cam.project(*self._model_point(i, d, float(levels[i]))) for i in range(self.n_bins)
            ]
            intensity = int(round(15.0 - 11.0 * d))  # front bright, back dim
            yield max(intensity, 3), points

    def draw(self, terminal: VectorTerminal, camera: Camera | None = None) -> None:
        """Render all traces into the terminal's current frame (back to front)."""
        for intensity, points in reversed(list(self.rows(camera))):
            terminal.set_intensity(intensity)
            terminal.polyline(points)


def step(
    source: SyntheticSource | AlsaSource,
    analyzer: Analyzer,
    waterfall: Waterfall3D,
    camera: Camera,
    detector: ChangeDetector,
    rotate: bool,
) -> bool:
    """Advance one frame: read audio, move the camera, push the new spectrum.

    Returns ``True`` if a major change fired this frame (so the camera jumped).
    """
    levels = analyzer.process(source.read_frame())
    jumped = False
    if rotate:
        jumped = detector.update(levels)
        if jumped:
            camera.jump()
        camera.update()
    waterfall.push(levels)
    return jumped


# --- animated-PNG preview -------------------------------------------------


def render_preview(
    path: str,
    source: SyntheticSource | AlsaSource,
    analyzer: Analyzer,
    waterfall: Waterfall3D,
    camera: Camera,
    detector: ChangeDetector,
    rotate: bool,
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
        step(source, analyzer, waterfall, camera, detector, rotate)
        with terminal.frame():
            waterfall.draw(terminal, camera)
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
    src.add_argument("--fmin", type=float, default=40.0, help="bottom of frequency axis (Hz)")
    src.add_argument("--fmax", type=float, default=DEFAULT_FMAX, help="top of frequency axis (Hz)")
    src.add_argument(
        "--tilt",
        type=float,
        default=3.0,
        help="spectral pre-emphasis (dB/octave) to tame bass dominance; "
        "0 = none, higher lifts the high end relative to the low",
    )

    disp = parser.add_argument_group("display")
    disp.add_argument("--bins", type=int, default=DEFAULT_BINS, help="frequency bins (X axis)")
    disp.add_argument("--history", type=int, default=DEFAULT_HISTORY, help="waterfall depth (rows)")
    disp.add_argument(
        "--fps",
        default="auto",
        help="target frames per second, or 'auto' (default) to let the device pace the stream",
    )
    disp.add_argument(
        "--scale",
        type=float,
        default=1.2,
        help="zoom the rendered image (1.0 = baseline). ~1.25 is the safe max "
        "with camera rotation; go to ~1.35 with --no-rotate. Larger clips the "
        "corners on loud, full-width frames at swing extremes",
    )
    disp.add_argument(
        "--no-rotate",
        action="store_true",
        help="hold a fixed head-on view (no drift, no perspective changes)",
    )
    disp.add_argument(
        "--sensitivity",
        type=float,
        default=DEFAULT_SENSITIVITY,
        help="change sensitivity: lower swings the view more often (higher = rarer)",
    )

    out = parser.add_argument_group("output")
    out.add_argument("--port", default=DEFAULT_PORT, help="serial device path")
    out.add_argument(
        "--baud",
        default="auto",
        help="line rate, or 'auto' (default) to detect the receiver's baud",
    )
    out.add_argument(
        "--no-flow-control",
        action="store_true",
        help="disable the per-frame handshake and just stream (for a buffered "
        "USB-CDC device; on by default for a raw-UART receiver like vekterm)",
    )
    out.add_argument("--dry-run", action="store_true", help="don't open a serial port")
    out.add_argument("--frames", type=int, default=0, help="frames to run (0 = forever)")
    out.add_argument("--preview", metavar="OUT.png", help="render an animated PNG and exit")
    out.add_argument("--width", type=int, default=480, help="preview width (px)")
    out.add_argument("--height", type=int, default=360, help="preview height (px)")
    debug.add_debug_argument(out)
    return parser.parse_args(argv)


def make_source(args: argparse.Namespace) -> SyntheticSource | AlsaSource:
    if args.synthetic or args.device is None:
        if not args.synthetic and args.device is None and not args.preview:
            print("No --device given; using the synthetic source (pass --device for live ALSA).")
        return SyntheticSource(args.rate, args.frame_size)
    return AlsaSource(args.device, args.rate, args.frame_size)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # fps "auto" -> None (the device paces the stream); else a numeric target.
    fps = None if str(args.fps).lower() == "auto" else float(args.fps)
    source = make_source(args)
    analyzer = Analyzer(
        args.rate,
        args.frame_size,
        n_bins=args.bins,
        f_min=args.fmin,
        f_max=args.fmax,
        tilt_db_per_oct=args.tilt,
    )
    waterfall = Waterfall3D(n_bins=args.bins, history=args.history)
    camera = Camera(size=CAM_SIZE * args.scale)
    detector = ChangeDetector(sensitivity=args.sensitivity)
    rotate = not args.no_rotate

    if args.preview:
        render_preview(
            args.preview,
            source,
            analyzer,
            waterfall,
            camera,
            detector,
            rotate,
            frames=args.frames or 120,
            fps=fps or 25.0,
            width=args.width,
            height=args.height,
        )
        return 0

    if args.dry_run:
        terminal = VectorTerminal(transport=MemoryTransport())
        print("[dry run] no serial port opened")
    else:
        if args.debug:
            print(f"Opening {args.port} at {args.baud} baud (waiting for the device to settle)...")
        flow = None if args.no_flow_control else DEFAULT_SYNC_BYTE
        terminal = VectorTerminal(port=args.port, baudrate=args.baud, flow_control=flow)

    reporter = debug.reporter_for(terminal, args.debug)
    drawn = 0
    try:
        while args.frames == 0 or drawn < args.frames:
            jumped = step(source, analyzer, waterfall, camera, detector, rotate)
            with terminal.frame():
                waterfall.draw(terminal, camera)
            if args.dry_run:
                last = terminal.transport.frames[-1]  # type: ignore[attr-defined]
                tag = " JUMP" if jumped else ""
                print(
                    f"frame {drawn:>4}: {len(last):>5} bytes, {waterfall.n_bins} bins, "
                    f"yaw={math.degrees(camera.yaw):+5.0f}deg{tag}"
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
