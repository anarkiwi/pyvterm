"""Render pyvterm frames as a glowing vector display — previews without hardware.

Optional module: needs the ``preview`` extra (``pip install "pyvterm[preview]"``,
i.e. numpy + Pillow). It is **not** imported by ``pyvterm`` itself, so the core
package stays dependency-light.

Frames are decoded from the *actual wire bytes* back into beam segments, so a
preview shows exactly what the device would draw. Capture frames with a
:class:`PreviewTransport`, then save an animated PNG::

    from pyvterm import VectorTerminal
    from pyvterm.preview import PreviewTransport

    preview = PreviewTransport(width=440, height=330)
    vt = VectorTerminal(transport=preview)
    for ...:
        with vt.frame():
            vt.polyline(points)
    preview.save_apng("out.png", fps=25)
"""

from __future__ import annotations

from collections.abc import Sequence

from . import protocol
from .protocol import DVG_RES_MAX, Flag
from .transport import Transport

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "pyvterm.preview needs the 'preview' extra: pip install 'pyvterm[preview]'"
    ) from exc

__all__ = ["Segment", "PHOSPHOR", "decode_segments", "rasterize", "save_apng", "PreviewTransport"]

#: A lit beam segment in device space: ``(x0, y0, x1, y1, intensity)``.
Segment = tuple[int, int, int, int, int]
Color = tuple[float, float, float]

#: Default phosphor tint (green-cyan glow with white-hot cores).
PHOSPHOR: Color = (0.18, 1.0, 0.55)


def decode_segments(frame: bytes) -> list[Segment]:
    """Decode a pyvterm frame into the lit segments the beam would draw.

    Coordinates are device space (``0..DVG_RES_MAX``). Blanked moves reposition
    the pen without producing a segment; a segment is emitted for each lit draw.
    """
    segments: list[Segment] = []
    pen_x = pen_y = 0
    intensity = 0
    for offset in range(0, len(frame) - 3, 4):
        info = protocol.decode_word(int.from_bytes(frame[offset : offset + 4], "big"))
        flag = info["flag"]
        if flag is Flag.RGB:
            intensity = max(info["r"], info["g"], info["b"])
        elif flag is Flag.XY:
            x, y = info["x"], info["y"]
            if not info["blank"] and intensity > 0:
                segments.append((pen_x, pen_y, x, y, intensity))
            pen_x, pen_y = x, y
    return segments


def rasterize(
    segments: Sequence[Segment], width: int, height: int, color: Color = PHOSPHOR
) -> Image.Image:
    """Rasterize device-space ``segments`` into a glowing RGB frame."""
    core = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(core)
    sx = (width - 1) / DVG_RES_MAX
    sy = (height - 1) / DVG_RES_MAX
    for x0, y0, x1, y1, intensity in segments:
        value = 90 + int(165 * intensity / 255)
        # Device Y grows upward, image Y downward -> flip.
        draw.line((x0 * sx, (height - 1) - y0 * sy, x1 * sx, (height - 1) - y1 * sy), fill=value)

    base = np.asarray(core, dtype=np.float32) / 255.0
    glow1 = np.asarray(core.filter(ImageFilter.GaussianBlur(2.0)), dtype=np.float32) / 255.0
    glow2 = np.asarray(core.filter(ImageFilter.GaussianBlur(5.0)), dtype=np.float32) / 255.0
    intensity_map = np.clip(base + 0.8 * glow1 + 0.5 * glow2 - 0.05, 0.0, 1.5)
    hot = np.clip((base - 0.45) * 2.2, 0.0, 1.0)  # white-hot line cores

    r, g, b = color
    rgb = np.zeros((height, width, 3), dtype=np.float32)
    rgb[..., 0] = r * intensity_map + 0.9 * hot
    rgb[..., 1] = g * intensity_map + 0.5 * hot
    rgb[..., 2] = b * intensity_map + 0.9 * hot
    out = (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
    return Image.fromarray(out, mode="RGB")


def save_apng(images: Sequence[Image.Image], path: str, fps: float = 25.0) -> int:
    """Write ``images`` as an animated PNG; returns the number of frames."""
    if not images:
        raise ValueError("no frames to save")
    duration = int(1000 / fps) if fps > 0 else 50
    images[0].save(
        path,
        save_all=True,
        append_images=list(images[1:]),
        duration=duration,
        loop=0,
        format="PNG",
    )
    return len(images)


class PreviewTransport(Transport):
    """A :class:`~pyvterm.transport.Transport` that captures frames as images.

    Drop it into a :class:`~pyvterm.VectorTerminal` in place of a serial port;
    each ``send_frame`` is captured, and :meth:`save_apng` writes the animation.
    """

    def __init__(self, width: int = 480, height: int = 360, color: Color = PHOSPHOR) -> None:
        self.width = width
        self.height = height
        self.color = color
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.frames.append(bytes(data))
        return len(data)

    def _frame_blobs(self) -> list[bytes]:
        # Keep only real frames (skip the trailing EXIT-only write from close()).
        return [
            blob
            for blob in self.frames
            if len(blob) >= 4
            and (int.from_bytes(blob[:4], "big") >> protocol.FLAG_SHIFT) == Flag.FRAME
        ]

    def images(self) -> list[Image.Image]:
        """Rasterize every captured frame to a list of images."""
        return [
            rasterize(decode_segments(blob), self.width, self.height, self.color)
            for blob in self._frame_blobs()
        ]

    def save_apng(self, path: str, fps: float = 25.0) -> int:
        """Render captured frames and write them to ``path`` as an animated PNG."""
        return save_apng(self.images(), path, fps=fps)
