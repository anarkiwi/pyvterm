# Protocol extensions — `vekterm` v2

> **Status: implemented.** This document specifies the *backward-compatible*
> v2 extensions to the USB-DVG / *vecterm* wire protocol for streaming
> structured, high-vector-count content (raster video, spectra, parametric
> curves). The base v1 protocol is in [`PROTOCOL.md`](PROTOCOL.md) and is
> unchanged: a sender only uses v2 after the device advertises it
> ([§3](#3-capability-negotiation-hello)). pyvterm encodes these
> ([`src/pyvterm/ext.py`](../src/pyvterm/ext.py)); vekterm decodes them
> (`src/protocol.c`, `src/frame.c`).

## 1. Motivation: the content doesn't look like Asteroids

The base protocol is an absolute, per-vector line-list with optional per-vector
colour. That suits arcade vector games — a few hundred arbitrary, individually
coloured strokes. The examples in this repo are a different shape: many vectors,
highly structured, temporally coherent. The mismatch is clearest as **degrees
of freedom per point** — the information each sample actually carries:

| Content | Real DOF / point | v1 wire floor | v2 primitive |
| --- | --- | --- | --- |
| arcade line-list (Asteroids) | 2 coords + blank + colour | ~4 B (`XY` + `RGB`) | base `XY` |
| Lissajous (parametric stroke) | 2 (x *and* y both vary) | ~2 B | [`POLYLINE`](#5-polyline-subtype-0x02) |
| Rutt-Etra / spectrum (function over a grid) | 1 (y only; x implicit) | ~1 B | [`HEIGHTFIELD`](#4-heightfield-subtype-0x01) |

The base protocol spends ~4 bytes per point regardless. The extensions let each
content type reach its information floor.

### 1.1 The budget: 1 Mbps

At 8N1 framing a byte costs 10 bits on the wire, so **1 Mbps = 100,000 B/s**.
The headline workload is `examples/ruttetra.py`'s default **44×24 grid ≈ 1056
points**. Measured bytes per frame, and the resulting frame-rate ceiling on a
1 Mbps link:

Measured by `examples/ruttetra.py --dry-run` (a full ``FRAME``/``COMPLETE``
envelope, so figures include the 12-byte frame overhead):

| Encoding | B/frame (44×24) | Words parsed | Max fps @ 100 KB/s |
| --- | --: | --: | --: |
| base absolute `XY` (v1) | 4240 | ~1085 | **23.6** |
| `HEIGHTFIELD`, y only (1 B/pt) | **1084** | 1 | **~92** |
| `HEIGHTFIELD` + intensity plane | 2140 | 1 | ~47 |
| frame suppressed (unchanged) | ~0 | 0 | 50 Hz-bound |

The base protocol barely streams the default grid at 1 Mbps (23.6 fps vs an
18 fps target, no headroom; any larger grid fails). `HEIGHTFIELD` gives ~4×
headroom; frame suppression ([§6](#6-frame-suppression)) takes the link out of
the critical path entirely for static or slow content. (The intensity plane
doubles the payload, so prefer it only when per-point luminance or threshold
gaps are worth it — and note the gaps also shrink the *base* frame.)

## 2. The `EXT` container

Opcode `6` (`0b110`) — unused in v1 and already silently ignored by vekterm's
parser — is spent once as an extensible, length-prefixed container rather than a
single new command:

```
 31    29 28        24 23                                   0
 +-------+------------+--------------------------------------+
 | 1 1 0 | subtype:5  | length:24  (payload byte count)      |
 +-------+------------+--------------------------------------+
 <length bytes of subtype-specific, byte-packed payload follow>

word    = (0x6 << 29) | ((subtype & 0x1F) << 24) | (length & 0xFFFFFF)
subtype = (word >> 24) & 0x1F
length  =  word & 0xFFFFFF
```

The 4-byte header is a normal big-endian command word, so it stays word-aligned
with the rest of the stream; the payload that follows is **raw bytes** (escaping
the rigid 32-bit-word model for bulk data). The 24-bit length lets a receiver
**skip a subtype it doesn't recognise** without losing word alignment, so the
format can grow without burning more opcodes. Cross-version safety still relies
on negotiation (§3): never send `EXT` to a device that didn't advertise v2.

| subtype | name | § |
| --- | --- | --- |
| `0x01` | `HEIGHTFIELD` | [4](#4-heightfield-subtype-0x01) |
| `0x02` | `POLYLINE` | [5](#5-polyline-subtype-0x02) |
| `0x03` | `HEIGHTFIELD_DELTA` *(reserved)* | [7](#7-deferred-temporal-deltas) |

All multi-byte payload fields are **big-endian**, matching the command words.
`i16`/`i8` are two's-complement signed.

## 3. Capability negotiation (`HELLO`)

A sender must discover whether the far end is a v2-capable vekterm (vs a plain
USB-DVG, or nothing). v1 reserves opcode `5` (`CMD`) as a device command channel
and vekterm ignores it, so it is a collision-free probe slot.

The probe is deliberately **binary, not JSON**: vekterm is a baremetal target
with no allocator, so the reply is a fixed 12-byte struct it can emit and the
sender can parse with zero allocation.

**Probe (host → device).** One `CMD` word with subcommand `0x56` (`'V'`),
distinct from AdvanceMAME's `GET_DVG_INFO = 1`:

```
word = (0x5 << 29) | 0x56          // = 0xA0000056
```

**Reply (device → host).** vekterm writes a fixed 12-byte descriptor:

| offset | field | value |
| --- | --- | --- |
| 0..1 | magic | `'V' 'K'` (`0x56 0x4B`) |
| 2 | protocol version | `2` |
| 3 | capability bitmap | bit0 `HEIGHTFIELD`, bit1 `POLYLINE`, bit2 intensity plane, bit3 frame-delta |
| 4 | coord bits | `12` |
| 5 | brightness bits | `8` |
| 6..7 | max pipeline (u16) | `3000` |
| 8..9 | max EXT payload (u16) | `8192` |
| 10 | refresh Hz | `50` |
| 11 | reserved | `0` |

**Sender logic** (`SerialTransport.probe_capabilities`). After the open/settle/
flush, the sender writes the probe and reads with a short timeout, scanning for
the `VK` magic. If found → enable the negotiated subtypes. On timeout or garbage
→ treat the device as base v1 and only ever send `XY`/`RGB` (exactly the existing
fall-back path for the flow-control sync byte). A plain USB-DVG ignores the `CMD`
word and never replies; AdvanceMAME's JSON reply fails the magic check — both
fall back safely. The probe coexists with the `0x06` flow-control sync byte
(§[PROTOCOL.md handshake]): the sender ignores `0x06` while scanning for `VK`.

## 4. `HEIGHTFIELD` (subtype `0x01`)

For content that genuinely is a function over a regular grid (Rutt-Etra rasters,
spectrum-analyzer rows). X is implicit, so each point costs ~1 byte.

```
header (16 bytes):
  flags:       u8    bit0 intensity-plane present
                     bit1 serpentine traversal
  cols:        u16   columns (points per row)
  rows:        u16   rows (scan lines)
  x0:          i16   device X of column 0
  x_step:      i16   per-column X increment (device units, signed)
  y0:          i16   device Y baseline of row 0
  y_step:      i16   per-row Y baseline increment (signed)
  y_scale:     u16   displacement byte 0..255 -> device units:
                     y = ybase + ((d * y_scale) >> 8)
  brightness:  u8    frame intensity (used when no intensity plane)
payload:
  displacement: rows*cols bytes (u8)                  ; the relief
  [intensity:   rows*cols bytes (u8)] if flags.bit0   ; optional per-point Z
```

Per-point Y is the **displacement magnitude** (not an inter-point delta), so
there is no overflow at sharp edges. 256 levels exceed the Vectrex's usable
position/brightness resolution.

**Expansion (identical on both ends).** For each row `r`, walk its columns
(forward, or reversed when `serpentine` and `r` is odd), compute device
`x = clamp(x0 + c·x_step)`, `y = clamp(y0 + r·y_step + ((d·y_scale)>>8))`. A
point whose intensity is `0` (only possible with an intensity plane) **breaks
the run** — that is how dark gaps and the `ruttetra` threshold are encoded for
free. Each consecutive lit pair within a run becomes one segment from the
previous point to this one, at the endpoint's intensity (or `brightness`). One
row is one polyline; a fresh row starts a fresh run (an implicit blanked move).

**Two wins beyond bytes**, because the receiver now holds the whole grid:

* **Serpentine traversal** (`flags.bit1`): emit alternate rows right-to-left, so
  the implicit move from the end of one row to the start of the next is short —
  removing ~`rows` long blanked retraces of analog beam time per frame.
* **Uniform step** lets the receiver hold one scale instead of re-deriving
  `GET_OPTIMAL_SCALE = max(|dx|,|dy|)/strength` per vector and thrashing the
  scale DAC.

## 5. `POLYLINE` (subtype `0x02`)

A general polyline as one absolute anchor plus signed deltas — serves *any*
stroke (Lissajous, arbitrary art, arcade geometry) and maps to the receiver's
native `v_directDeltaDraw32`.

```
header (8 bytes):
  flags:       u8    bit0 intensity-present  bit1 closed  bit2 16-bit deltas
  brightness:  u8    default beam intensity (used when no per-point intensity)
  x0:          u16   absolute device start X (0..4095)
  y0:          u16   absolute device start Y
  count:       u16   number of points, including the start
payload: (count - 1) entries of
  dx, dy:      i8 each   (i16 each if flags.bit2)
  [intensity:  u8]       if flags.bit0
```

**Expansion.** Start at `(x0, y0)`; accumulate each `(dx, dy)`, clamp to
`0..4095`, and emit a segment from the previous point at the entry's intensity
(or `brightness`). An entry with intensity `0` is a blanked move (no segment,
beam repositioned). With `closed`, a final segment returns to `(x0, y0)` at
`brightness`. 8-bit deltas cost **2 B/pt** (+1 B with intensity); steps beyond
±127 require the 16-bit escape (`bit2`), at which point `POLYLINE` degrades to
absolute and `HEIGHTFIELD` or finer sampling is the better tool.

Accumulated 8-bit deltas drift; re-anchor with a fresh `POLYLINE` (or send the
closing segment absolute) every ~64 points for closed loops.

## 6. Frame suppression

The receiver redraws the **active frame** every refresh until a new `COMPLETE`
arrives, and the per-frame flow-control handshake already tolerates a sender
that stays silent (it times out and redraws the held frame). So the cheapest
temporal delta needs **no protocol change at all**: when a frame is byte-identical
to the one last sent, the sender simply *doesn't send it*. pyvterm does this when
`VectorTerminal(suppress_duplicates=True)`: `send_frame` compares against the last
transmitted bytes and skips the write on a match, and reports it via
`frames_suppressed`. For a static or slowly changing scene the link goes idle and
the 50 Hz analog redraw becomes the only cost.

## 7. Deferred: temporal deltas

`HEIGHTFIELD_DELTA` (`0x03`) would reference the previous frame of identical
topology and RLE only the changed displacement samples — a large win for
video-like Rutt-Etra streaming, marginal for fast morphs. The opcode and
capability bit are reserved; the encoder/decoder are not yet implemented.
Frame suppression (§6) already captures the all-or-nothing case.

## 8. Receiver CPU: map once per frame, not once per refresh

The dominant receiver cost is not parsing — it is the **50 Hz redraw** of every
segment, each calling `vt_map_coord` (a 64-bit multiply/divide) to map device
`0..4095` onto the Vectrex integrator range. For 1056 segments that is ~53k
expensive ops/sec on coordinates that never change between refreshes. The
baremetal receiver therefore maps each frame **once**, when it becomes active
(`on_frame`), into a precomputed draw list the refresh loop renders verbatim.
This is independent of the wire format and stacks with the ~1000× fewer command
dispatches `HEIGHTFIELD` brings (1 word vs ~1085).

## 9. Limits the protocol cannot beat

* **Analog beam settling** — each vector costs tens of microseconds; this bounds
  *frame rate at a given vector count* regardless of encoding.
* **`MAX_PIPELINE = 3000`** — the receiver buffers at most 3000 vectors/frame.

So the largest fps lever is still **reducing vector count** (resolution), a
content decision. The protocol's job is to cut transfer bytes (~4×), cut receiver
dispatches (~1000×), draw a frame faster (serpentine + fixed scale), unlock
per-point intensity, and — via suppression/deltas — take the link out of the
critical path for streaming content.

## 10. Rollout / value order

1. **Frame suppression** (§6) + **map-once** (§8) — zero/one-sided, ship first.
2. **`HELLO` negotiation** (§3) — the enabler.
3. **`HEIGHTFIELD`** (§4) — the Rutt-Etra workhorse, ~4× at 1 Mbps.
4. **`POLYLINE`** (§5) — universal ~2× for non-grid strokes.
5. **Temporal deltas** (§7) — deferred.

## 11. Keepalive (`CMD` subcommand `'K'`)

The baremetal receiver runs a **global inactivity timeout** (30 s by default):
with no new frame for that long it drops the held frame and returns to its
splash, so a stale image never lingers after a sender disconnects or crashes.
That interacts badly with **frame suppression** (§6): a legitimately *static*
scene sends nothing, and would be mistaken for a dead sender.

The keepalive resolves it. It is a single `CMD` word with subcommand `'K'`
(`0x4B`) — collision-free in the same slot as the `HELLO` probe (`'V'`):

```
word = (0x5 << 29) | 0x4B          // = 0xA000004B
```

It carries no geometry: the receiver treats it as proof the sender is alive
(resetting the timeout) **without changing the drawn frame**, and it satisfies
the per-frame flow-control handshake exactly like a frame would (so the receiver
issues the next sync promptly). A suppressing sender emits one keepalive per
handshake while the scene is unchanged. pyvterm exposes
`protocol.keepalive()` and `VectorTerminal.send_keepalive()`; vekterm decodes it
via `vt_is_keepalive` and a new `on_keepalive` sink callback. A v1 device ignores
the unknown `CMD` subcommand, so the ping is harmless to send blindly.

## 12. Frame-timing in the sync reply (adaptive frame rate)

The per-frame flow-control reply (the receiver's "ready for the next frame"
signal) is upgraded, **once v2 is negotiated**, to carry **how long the receiver
took to draw the last frame**, so a sender can adapt its frame rate to the
scene's complexity rather than guessing a fixed fps.

This is an *efficient* change, not a back-compatible one: rather than bolting a
record onto the base `0x06` sync byte, a negotiated v2 receiver replaces the
sync byte **wholesale** with a compact fixed 5-byte record. Its arrival is the
readiness signal — there is no sync byte and no marker to spend bytes on:

```
 byte 0..1  draw_us  u16   microseconds spent drawing the last frame (BE)
 byte 2..3  vectors  u16   vectors in the last frame (BE)
 byte 4     flags    u8    bit0 overflow, bit1 idle (splash drawn)
```

Cross-version safety comes from **negotiation, not framing**, exactly like the
EXT subtypes (§2): the receiver only sends the record to a peer that issued the
`HELLO` probe; an un-negotiated peer keeps getting the plain `0x06` sync byte, so
the base DVG flow control ([`PROTOCOL.md`](PROTOCOL.md) handshake) is untouched.

`draw_us` is the analog beam time the held frame costs **per refresh** — the real
ceiling on frame rate (§9), which the wire byte-budget tables don't capture.
pyvterm reads the record in `SerialTransport` (switched on by a successful
`probe_capabilities`) and surfaces it as `VectorTerminal.last_timing` (a
`FrameTiming` with a `max_fps` helper); a sender throttles with e.g.
`sleep(max(0, target_dt - draw_us/1e6))` (see `examples/ruttetra.py
--adaptive-fps`). The `idle` flag lets a sender notice the receiver fell back to
its splash (timeout, §11) and restart the stream. vekterm emits the record from
`send_sync_reply` in the baremetal draw loop, timing the draw with the 1 MHz
system timer.

## References

* Base wire protocol — [`PROTOCOL.md`](PROTOCOL.md)
* Sender encoders — [`src/pyvterm/ext.py`](../src/pyvterm/ext.py)
* Receiver decoders — vekterm `src/protocol.c`, `src/frame.c`
* Receiver primitives — `pitrex/vectrex/vectrexInterface.h`
  (`v_directDeltaDraw32`, `v_directDraw32(..., brightness)`, `v_setScale`,
  `GET_OPTIMAL_SCALE`, `MAX_PIPELINE`)
* Examples — [`ruttetra.py`](../examples/ruttetra.py),
  [`lissajous.py`](../examples/lissajous.py),
  [`spectrum3d.py`](../examples/spectrum3d.py)
</content>
</invoke>
