# Protocol extensions (proposal)

> **Status: design proposal, not implemented.** This document sketches
> *backward-compatible* extensions to the USB-DVG / *vecterm* wire protocol for
> streaming structured, high-vector-count content (raster video, spectra,
> parametric curves). The protocol as actually shipped today is specified in
> [`PROTOCOL.md`](PROTOCOL.md); nothing here changes that until a device
> advertises support (see [§5](#5-capability-negotiation)).

## 1. Motivation: the content doesn't look like Asteroids

The base protocol ([`PROTOCOL.md`](PROTOCOL.md)) is an absolute, per-vector
line-list with optional per-vector colour. That suits arcade vector games — a
few hundred arbitrary, individually-coloured strokes. The examples in this repo
are a different shape: many vectors, highly structured, temporally coherent.

The cleanest way to see the mismatch is **degrees of freedom per point** — the
information each sample actually carries:

| Content | Real DOF / point | Wire floor | Best primitive |
| --- | --- | --- | --- |
| arcade line-list (Asteroids) | 2 coords + blank + colour | ~4 B (`XY` + `RGB`) | base `XY` |
| Lissajous (parametric stroke) | 2 (x *and* y both vary) | ~2 B | [`STRIP`](#6-strip--packed-relative-polyline) |
| spectrum waterfall / Rutt-Etra (function over a grid) | 1 (y only; x implicit) | ~1 B | [`HEIGHTFIELD`](#7-heightfield--gridded-scan) |

The base protocol spends ~4 bytes per point regardless. The two extensions
below let each content type reach its information floor, and they are
**complementary** — neither subsumes the other (see [§11](#11-per-example-mapping--rollout)).

### 1.1 Two concrete workloads

* **Rutt-Etra raster** ([`examples/ruttetra.py`](../examples/ruttetra.py)) — a
  44x24 grid is ~1060 words = **4240 B/frame**. X is a fixed grid; Y is
  baseline + small luminance displacement; intensity is set once because
  per-point intensity would double the stream; frames are nearly identical.
* **Lissajous** ([`examples/lissajous.py`](../examples/lissajous.py)) — one
  smooth closed stroke of 401 points = **1620 B/frame**. X *and* Y both vary
  (the curve revisits every X), so it is **not** a function over a grid;
  consecutive samples are close because the curve is finely sampled.

## 2. Why the base protocol struggles here

Recapping the relevant facts from [`PROTOCOL.md`](PROTOCOL.md):

1. **Absolute coords, every point.** 32 bits/point even when the real content
   is ~8 bits (an implicit X grid, or a small delta from the last point).
2. **Framing tax.** 4 of 32 bits per point are flag + blank (12.5%), and
   coords are 14-bit fields into a 12-bit device (4 more wasted bits/point).
3. **Colour is a 4-byte sidecar** (`RGB` word), so per-point intensity is
   unaffordable — even though the receiver's `v_directDraw32(..., brightness)`
   and `v_directDeltaDraw32(dx, dy, brightness)` take brightness for free.
4. **No temporal delta** — an unchanged frame is retransmitted in full.
5. **The sender fixes traversal** as a flat list, so the receiver cannot
   reorder to serpentine (it pays a blanked retrace per scan line) or hold a
   single scale (it re-runs `GET_OPTIMAL_SCALE = max(|dx|,|dy|)/strength` per
   vector, thrashing the scale DAC) even when the step is uniform.

## 3. Design principles

* **Backward compatible.** Old devices keep working; extensions are used only
  after the device advertises them.
* **Capability-negotiated**, via the existing `CMD`/`GET_DVG_INFO` channel.
* **Byte-packed payloads**, escaping the rigid 32-bit-word model for bulk data.
* **Map onto the receiver's native primitives** — relative draws
  (`v_directDeltaDraw32`), per-vector brightness, a single scale register — so
  the receiver does *less* work, not more.
* **Sender falls back** to plain `XY` when a capability is absent, so encoders
  can be written and tested today against an unmodified vecterm.

## 4. The `EXT` container

Only opcode `6` (`0b110`) is unused, so spend it once as an extensible,
length-prefixed container rather than a single new command:

```
 31    29 28        24 23                                   0
 +-------+------------+--------------------------------------+
 | 1 1 0 | subtype:5  | length:24  (payload byte count)      |
 +-------+------------+--------------------------------------+
 <length bytes of subtype-specific, byte-packed payload follow>
```

The 24-bit length lets a v2-aware receiver **skip subtypes it doesn't
recognise**, so the format can grow without further opcode burn. Cross-version
safety still relies on negotiation (§5): never send `EXT` to a device that
didn't advertise v2. Proposed subtypes:

| subtype | name | §|
| --- | --- | --- |
| `0x01` | `STRIP` | [6](#6-strip--packed-relative-polyline) |
| `0x02` | `HEIGHTFIELD` | [7](#7-heightfield--gridded-scan) |
| `0x03` | `HEIGHTFIELD_DELTA` | [8](#8-temporal-deltas) |
| `0x04` | `STRIP_DELTA` | [8](#8-temporal-deltas) |
| `0x05` | `CURVE` (speculative) | [10](#10-speculative-curve--spline) |

## 5. Capability negotiation

Extend the `GET_DVG_INFO` JSON reply so the sender can probe and adapt:

```json
{
  "device": "pitrex-vecterm",
  "protocol": 2,
  "extensions": ["strip", "heightfield", "heightfield_delta"],
  "coord_bits": 12,
  "max_pipeline": 3000
}
```

A device that doesn't answer, or omits `extensions`, is treated as base
protocol and only sees `XY`/`RGB`.

## 6. `STRIP` — packed relative polyline

A general polyline as one absolute anchor plus signed deltas. This is the
**foundational** extension: it serves *any* stroke (Lissajous, arbitrary art,
arcade geometry) and maps directly to `v_directDeltaDraw32`.

```
header:
  flags:      u8    bit0 intensity-present  bit1 closed  bit2 16-bit deltas
  scale:      u16   Vectrex scale register for the whole strip
  brightness: u8    default beam intensity (if no per-point intensity)
  x0, y0:     i16   absolute device start point (0..4095)
  count:      u16   number of points (including the start)
payload: (count - 1) entries of
  dx, dy:     i8    (i16 each if flags.bit2)
  [intensity: u8]   if flags.bit0
```

**Cost:** 2 B/point (8-bit deltas) [+1 B optional intensity], vs 4 B absolute.

**Fit — Lissajous (good, with caveats).** The default curve is sampled finely
enough that the largest step is small: `A·a·Δt ≈ 1922·3·(2π/400) ≈ 90` device
units in X, ~60 in Y — inside a signed 8-bit delta. So 401 points ≈ **~810 B
(~50%)** in one command instead of ~405 words.

* *Conditional on sampling.* Halving `--samples` or raising `-a/-b` pushes the
  step past ±127, forcing the 16-bit escape — at which point `STRIP` degrades
  to absolute. Win requires step < ~128 device units.
* *Accumulation drift.* Summed 8-bit deltas accumulate rounding error, so a
  closed loop may not exactly re-meet its start. Re-anchor with an absolute
  point periodically (e.g. every 64 points) or send the closing segment
  absolute.
* *Scale.* Lissajous deltas vary ~4x (near-zero at turning points, max at
  zero-crossings); the single per-strip `scale` is sized for the max, so small
  segments lose a little resolution. Acceptable, and the opposite of
  `HEIGHTFIELD`'s clean uniform step.

`HEIGHTFIELD` does **not** apply to Lissajous: there is no implicit X axis to
drop (the curve is not `y = f(x)`).

## 7. `HEIGHTFIELD` — gridded scan

For content that genuinely is a function over a regular grid (Rutt-Etra rasters,
spectrum-analyzer rows). X is implicit, so each point costs ~1 byte.

```
header:
  flags:      u8    bit0 intensity-plane  bit1 serpentine-ok  bit2 4-bit intensity
  cols, rows: u16
  x0, x_step: i16   device X of column 0, and per-column increment
  y0, y_step: i16   device Y baseline of row 0, and per-row baseline increment
  y_scale:    u16   displacement byte 0..255 -> device units (y = base + (d*y_scale)>>8)
  scale:      u16   Vectrex scale register
  brightness: u8    frame intensity (if no intensity plane)
payload:
  displacement: rows*cols bytes (u8)                     ; the relief
  [intensity:   rows*cols bytes, or *4 bits if flags.bit2]  ; optional per-point Z
```

Per-point Y is the **displacement magnitude** (not an inter-point delta), so
there is no overflow problem at sharp edges; 256 levels exceed the Vectrex's
usable position/brightness resolution.

| Encoding (44x24) | Bytes/frame | Words parsed | Per-point brightness |
| --- | --: | --: | :--: |
| base absolute `XY` | 4240 | ~1060 | no (would double) |
| `STRIP` per row (Δ, 2 B/pt) | ~2220 | 24 | no |
| `HEIGHTFIELD`, Y only | **~1070** | 1 | no |
| `HEIGHTFIELD` + 4-bit intensity | ~1600 | 1 | **yes** |

Two wins beyond bytes, because the receiver now holds the whole grid:

* **Serpentine traversal** (`flags.bit1`): draw row L→R, next row R→L, removing
  ~`rows` blanked retraces of *analog beam time*.
* **Uniform scale**: set the scale register once for the whole field instead of
  per-vector `GET_OPTIMAL_SCALE`, avoiding scale-DAC settling.

## 8. Temporal deltas

`HEIGHTFIELD_DELTA` (`0x03`) / `STRIP_DELTA` (`0x04`) reference the previous
frame of identical topology and send only what changed. Sketch for the
heightfield case — RLE over the displacement plane:

```
tokens, repeated until length consumed:
  [skip: varint]                 ; this many points unchanged from last frame
  [run:  varint][d: i8 ...]      ; this many changed points, signed deltas
```

The receiver keeps one previous plane and applies deltas.

* **Rutt-Etra / video: large win.** Static or slow regions collapse to near
  zero bytes — this is where continuous streaming pays off.
* **Lissajous: marginal.** Topology is stable, but at the default morph speed
  each point moves `A·dδ ≈ 1922·(2π/120) ≈ 100` device units/frame — *as large
  as* the within-frame `STRIP` deltas — so a temporal delta is no smaller and
  adds previous-frame state. Worthwhile only for very slow animations.

## 9. Optional: lightweight payload compression

Any `EXT` payload may be wrapped in a cheap codec (delta + RLE, or LZ4) when
the device advertises it. LZ4 decode is affordable on a Pi Zero; avoid heavier
schemes (deflate) on the baremetal target. This stacks on top of the structural
gains above and is most effective on the smooth, repetitive displacement
planes.

## 10. Speculative: `CURVE` / spline

For inherently smooth curves (Lissajous is the archetype), send a handful of
control points (Catmull-Rom / Bézier) and let the receiver tessellate — a dozen
control points instead of 400 deltas. This is the "Lissajous-shaped" primitive,
the way `HEIGHTFIELD` is the "Rutt-Etra-shaped" one. The cost is real
receiver-side fixed-point evaluation per frame, and it only helps smooth
content, so it is a tier-3 idea rather than a near-term ship.

## 11. Per-example mapping & rollout

| Example | Shape | Primitive |
| --- | --- | --- |
| Lissajous | parametric closed stroke | `STRIP` |
| 3D spectrum analyzer | rows of `y = f(x)` | `HEIGHTFIELD` (or `STRIP`/row) |
| Rutt-Etra | raster `y = f(x)` grid | `HEIGHTFIELD` (+ intensity / temporal delta) |

Recommended order of value:

1. **`EXT` container + capability negotiation** — the enabler.
2. **`STRIP`** — universal, ~2x everywhere, hardware-delta-native. If only one
   extension ships, this is it.
3. **`HEIGHTFIELD`** (+ intensity, serpentine, uniform scale) — doubles the win
   again for grid-shaped content; unlocks per-point luminance.
4. **Temporal deltas** — for video-like streaming (Rutt-Etra), not for Lissajous.
5. **`CURVE`** — speculative.

## 12. Limits the protocol cannot beat

State plainly: the wire format does not change the physics.

* **Analog beam settling** — each vector costs tens of microseconds for the
  integrators to settle; this bounds *frame rate at a given vector count*.
* **`MAX_PIPELINE = 3000`** — the receiver buffers at most 3000 vectors/frame,
  so resolution is capped regardless of encoding (a 64x48 grid already exceeds
  it).

So the largest fps lever remains **reducing vector count** (resolution), which
is a content decision. The protocol's job is to cut transfer bytes (~4x), cut
receiver parse work (~1000x fewer dispatches), draw a given frame faster
(serpentine + fixed scale), unlock per-point intensity, and make video viable
via temporal deltas.

## 13. Fallback & a pyvterm implementation path

Because each extension has an exact expansion to base `XY`/`RGB`, an encoder can
be built and tested today:

* Add `pyvterm` encoders for `STRIP` / `HEIGHTFIELD` that emit `EXT` payloads.
* Provide a **software fallback** that expands them to plain `XY` frames, so the
  output is byte-identical-in-effect on an unmodified vecterm and unit-testable
  against [`PROTOCOL.md`](PROTOCOL.md).
* Gate the real `EXT` path on a capability flag (default off).
* Wire `--strip` / `--heightfield` switches into the examples to measure the
  byte savings end-to-end.

## References

* Base wire protocol — [`PROTOCOL.md`](PROTOCOL.md)
* Receiver primitives — `pitrex/vectrex/vectrexInterface.h`
  (`v_directDeltaDraw32`, `v_directDraw32(..., brightness)`, `v_setScale`,
  `GET_OPTIMAL_SCALE`, `MAX_PIPELINE`)
* Examples — [`lissajous.py`](../examples/lissajous.py),
  [`spectrum3d.py`](../examples/spectrum3d.py),
  [`ruttetra.py`](../examples/ruttetra.py)
