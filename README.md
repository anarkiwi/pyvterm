# pyvterm

[![CI](https://github.com/anarkiwi/pyvterm/actions/workflows/ci.yml/badge.svg)](https://github.com/anarkiwi/pyvterm/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pyvterm.svg)](https://pypi.org/project/pyvterm/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyvterm.svg)](https://pypi.org/project/pyvterm/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Drive a [PiTrex](https://github.com/gtoal/pitrex)/Vectrex over a serial port from Python.**

pyvterm speaks the **USB-DVG / _vecterm_ serial protocol** — the same wire format a
custom [MAME](https://www.mamedev.org/) build uses to push vector frames to a Vectrex
through the PiTrex. With pyvterm your Python program *becomes* the "custom MAME": you
build a frame of vectors and stream it to real hardware over a serial link.

The protocol is documented in full in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).

---

## How it fits together

```
┌─────────────────────────┐   USB-DVG / vecterm    ┌──────────────────┐   GPIO/VIA   ┌─────────┐
│  your Python program     │   protocol over a      │  PiTrex running  │  6522 VIA    │ Vectrex │
│  (pyvterm)  ── or ──      │ ─────────────────────▶ │  the "vecterm"   │ ───────────▶ │ CRT     │
│  a custom MAME build      │   serial @ 2 Mbaud     │  receiver        │              │ (beam)  │
└─────────────────────────┘                         └──────────────────┘              └─────────┘
```

pyvterm implements the **sender** half — exactly what
[`VMMenu/Win32/dvg/zvgFrame.c`](https://github.com/gtoal/pitrex/blob/master/VMMenu/Win32/dvg/zvgFrame.c)
does in the PiTrex repository (USB-DVG drivers by Mario Montminy, 2020), cross-checked
against AdvanceMAME's canonical [`advance/osd/dvg.c`](https://github.com/amadvance/advancemame/blob/master/advance/osd/dvg.c).

## Install

```bash
pip install pyvterm
```

pyvterm requires Python 3.9+ and depends only on [`pyserial`](https://pypi.org/project/pyserial/).

## Quick start

```python
from pyvterm import VectorTerminal

# Open the serial link (USB-CDC device shows up as /dev/ttyACM0 on Linux).
with VectorTerminal(port="/dev/ttyACM0") as vt:
    with vt.frame():                 # clears, then sends on exit
        vt.set_intensity(15)          # full brightness (0 = beam off)
        vt.polyline(                  # a centred square
            [(-200, -200), (200, -200), (200, 200), (-200, 200)],
            closed=True,
        )
```

No hardware handy? Swap in a `MemoryTransport` and inspect the bytes:

```python
from pyvterm import VectorTerminal, MemoryTransport, protocol

mem = MemoryTransport()
vt = VectorTerminal(transport=mem)
vt.set_intensity(15)
vt.draw_to(100, 0)                    # pen starts at (0, 0)
frame = vt.send_frame()
print([protocol.decode_word(int.from_bytes(frame[i:i+4], "big"))
       for i in range(0, len(frame), 4)])
```

## Coordinate system

The default host space matches MAME's vector resolution and the PiTrex `zvgFrame.h`
defaults: **X ∈ [−512, 511], Y ∈ [−384, 383]**, origin at centre. pyvterm maps these
onto the device's `0..4095` grid for you. Pass a custom `Bounds` to `VectorTerminal`
if you want different limits.

The Vectrex CRT is monochrome, so colour is really *intensity*: use
`set_intensity(0..15)`. A colour/intensity of `0` blanks the beam, turning the next
vector into an invisible move. (`set_rgb(r, g, b)` is available for protocol fidelity
with colour vector monitors.)

## API at a glance

| pyvterm | `zvgFrame.c` equivalent | Purpose |
| --- | --- | --- |
| `VectorTerminal(port=...)` / `.open(port)` | `zvgFrameOpen` | open the serial link |
| `.set_rgb(r, g, b)` / `.set_intensity(n)` | `zvgFrameSetRGB15` | set colour/brightness |
| `.set_clip_window(...)` | `zvgFrameSetClipWin` | set the clip rectangle |
| `.vector(x0, y0, x1, y1)` | `zvgFrameVector` | add one vector |
| `.move_to` / `.draw_to` / `.polyline` | — | pen-style convenience helpers |
| `.send_frame()` | `zvgFrameSend` | serialise + transmit the frame |
| `.close()` | `zvgFrameClose` | send `EXIT`, close the port |

Lower-level building blocks are exposed too: `pyvterm.protocol` (pure word
encoders/decoders), `pyvterm.geometry` (clipping), `FrameBuilder` (assemble a frame
to bytes without any I/O), and the `Transport` hierarchy
(`SerialTransport`, `MemoryTransport`).

## Examples

### Lissajous patterns

[`examples/lissajous.py`](examples/lissajous.py) animates Lissajous curves on the
display:

```bash
# On real hardware:
python examples/lissajous.py --port /dev/ttyACM0

# Without hardware (prints per-frame byte counts):
python examples/lissajous.py --dry-run --frames 5
```

### 3D spectrum analyzer

[`examples/spectrum3d.py`](examples/spectrum3d.py) is a real-time **3D waterfall
spectrum analyzer**: it captures live audio (ALSA), runs an FFT each frame, and
draws frequency across X, magnitude as height, and time receding into the
distance.

![3D waterfall spectrum analyzer preview](docs/spectrum3d.png)

*Animated preview (open the PNG to play it) rendered by `--preview` from the
built-in synthetic source — exactly the vectors the device would draw,
reconstructed from the wire bytes.*

```bash
# Live, visualising the default output by tapping its monitor
# (Linux; needs pyalsaaudio):
pip install "pyvterm[analyzer]" pyalsaaudio
PULSE_SOURCE=@DEFAULT_SINK@.monitor python examples/spectrum3d.py --device pulse

# No hardware? Render the animated PNG above from synthetic audio:
pip install "pyvterm[preview]"
python examples/spectrum3d.py --synthetic --preview spectrum3d.png

# Or just stream synthetic audio to a real Vectrex:
python examples/spectrum3d.py --synthetic --port /dev/ttyACM0
```

## Hardware notes

- The PiTrex/USB-DVG enumerates as a USB-CDC ACM device: `/dev/ttyACM0` (Linux),
  `/dev/tty.usbmodemXXXX` (macOS), or `COMx` (Windows).
- The nominal line rate is 2 Mbaud. USB-CDC ignores the rate, but pyvterm requests it
  to match the reference driver.
- On open, `SerialTransport` waits ~2 s before flushing (the reference driver does the
  same "to make flush work, for some reason"). Pass `settle=0` to skip it.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest
```

## Credits

The protocol and the PiTrex platform are the work of Graham Toal and contributors
([gtoal/pitrex](https://github.com/gtoal/pitrex)); the USB-DVG drivers this library
mirrors were written by Mario Montminy. pyvterm is an independent, clean-room Python
reimplementation of the sender protocol.

## License

Apache-2.0. See [LICENSE](LICENSE).
