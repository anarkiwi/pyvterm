# The USB-DVG / *vecterm* serial protocol

This document specifies the serial wire protocol that
[`gtoal/pitrex`](https://github.com/gtoal/pitrex) uses to let a **custom MAME
build drive a PiTrex/Vectrex over a serial port**, and which `pyvterm`
implements on the sending side.

It is reconstructed from the reference sender in the PiTrex repository,
[`VMMenu/Win32/dvg/zvgFrame.c`](https://github.com/gtoal/pitrex/blob/master/VMMenu/Win32/dvg/zvgFrame.c)
(USB-DVG drivers by Mario Montminy, 2020), and cross-checked against the
canonical AdvanceMAME implementation,
[`advance/osd/dvg.c`](https://github.com/amadvance/advancemame/blob/master/advance/osd/dvg.c).

> **Provenance.** The protocol originated with Mario Montminy's *USB-DVG*
> (a card that drives real colour vector monitors from MAME). The PiTrex
> reuses it: a PiTrex running the *vecterm* receiver pretends to be a USB-DVG,
> so a USB-DVG-aware MAME (or `pyvterm`) can paint vectors on a Vectrex.

---

## 1. Architecture

```
   sender (this protocol)                          receiver
 ┌───────────────────────────┐   serial bytes   ┌────────────────────────┐
 │ custom MAME / pyvterm      │ ───────────────▶ │ PiTrex "vecterm"        │
 │ builds a frame of vectors  │   @ ~2 Mbaud     │ decodes + drives the    │
 │ and streams command words  │                  │ Vectrex beam (6522 VIA) │
 └───────────────────────────┘                  └────────────────────────┘
```

The link is **one-way for drawing** (host → device). The richer AdvanceMAME
variant also has a small device→host channel for capability discovery
(§7); the PiTrex `zvgFrame.c` variant does not use it.

## 2. Serial port settings

| Setting | Value |
| --- | --- |
| Device | `/dev/ttyACM0` (Linux), `/dev/tty.usbmodem*` (macOS), `COMx` (Windows) |
| Nominal baud rate | `2000000` (2 Mbaud) |
| Data bits | 8 |
| Parity | none |
| Stop bits | 1 |
| Flow control | none (no XON/XOFF, no RTS/CTS, no DSR/DTR gating) |
| Mode | raw (`cfmakeraw`, `CLOCAL | CREAD`, `~OPOST`) |

The device is a **USB-CDC ACM** gadget, so the baud rate is nominal — the USB
link runs at full/high speed regardless — but the reference driver requests
2 Mbaud and `pyvterm` does the same.

The reference Linux driver sleeps ~2 seconds after opening, then flushes both
buffers (`tcflush(fd, TCIOFLUSH)`) "to make flush work, for some reason".
`pyvterm`'s `SerialTransport` reproduces this (`settle=2.0`, then
`reset_input_buffer()` / `reset_output_buffer()`).

## 3. Command word format

Every command is a single **32-bit word, transmitted big-endian** (most
significant byte first). The top three bits select the command:

```
 bit  31      29 28                                              0
      +---------+------------------------------------------------+
      |  flag   |  payload (command-specific)                    |
      +---------+------------------------------------------------+
```

```c
s_cmd_buf[off++] = cmd >> 24;
s_cmd_buf[off++] = cmd >> 16;
s_cmd_buf[off++] = cmd >>  8;
s_cmd_buf[off++] = cmd >>  0;
```

### Flags

| Name | Value (`flag`) | Word prefix (`flag << 29`) | Meaning |
| --- | --- | --- | --- |
| `COMPLETE` | `0x0` | `0x00000000` | End-of-frame marker |
| `RGB` | `0x1` | `0x20000000` | Set colour of following vectors |
| `XY` | `0x2` | `0x40000000` | Move (beam off) or draw (beam on) |
| `QUALITY` | `0x3` | `0x60000000` | Render-quality hint *(zvgFrame variant)* |
| `FRAME` | `0x4` | `0x80000000` | Frame header w/ total beam length *(zvgFrame variant)* |
| `CMD` | `0x5` | `0xA0000000` | Device command channel *(AdvanceMAME)* |
| `EXIT` | `0x7` | `0xE0000000` | Session over |

## 4. Commands

### 4.1 `FRAME` — frame header (zvgFrame variant)

```
 31      29 28                                              0
 +---------+------------------------------------------------+
 |  1 0 0  |  vector_length (29 bits)                       |
 +---------+------------------------------------------------+

cmd = (FLAG_FRAME << 29) | (vector_length & 0x1FFFFFFF)
```

`vector_length` is the **total beam travel** of the frame: the running sum,
over every drawn vector, of the distance from the previous endpoint to this
vector's start plus the length of the vector itself. The device uses it to
pace the frame. It is written into the first four bytes of every frame.

### 4.2 `RGB` — set colour / intensity

```
 31      29 23      16 15       8 7        0
 +---------+----------+----------+----------+
 |  0 0 1  |    red   |   green  |   blue   |
 +---------+----------+----------+----------+

cmd = (FLAG_RGB << 29) | ((r & 0xff) << 16) | ((g & 0xff) << 8) | (b & 0xff)
```

In `zvgFrameSetRGB15`, each input channel is scaled `value << 4` and clamped
to 255 (so 0–15 maps to 0–240, and ≥16 saturates at 255). A colour of
`(0, 0, 0)` is **black**, which blanks subsequent draws — see §5.

The Vectrex CRT is monochrome; only luminance matters, so in practice you set
`r == g == b` (an *intensity*).

### 4.3 `XY` — move or draw

```
 31      29 28 27                14 13                 0
 +---------+--+---------------------+--------------------+
 |  0 1 0  |bl|     x (14 bits)     |     y (14 bits)    |
 +---------+--+---------------------+--------------------+

cmd = (FLAG_XY << 29) | ((blank & 1) << 28) | ((x & 0x3fff) << 14) | (y & 0x3fff)
```

* **`blank` (bit 28)** — `1` = beam **off** (a move), `0` = beam **on** (a draw).
* **`x`, `y`** — 14-bit device coordinates in the range `0..4095` (§6).

An `XY` word names a *target*; the beam travels there from its previous
position, drawing iff `blank == 0`. So a visible line is "(optional blanked
move to the start) then (lit `XY` to the end)".

### 4.4 `QUALITY` — render-quality hint (zvgFrame variant)

```
cmd = (FLAG_QUALITY << 29) | (value & 0x1FFFFFFF)     // value = 5 by default
```

Sent once per frame, just before `COMPLETE`.

### 4.5 `COMPLETE` — end of frame

```
cmd = (FLAG_COMPLETE << 29)        // = 0x00000000
```

Marks the end of the command stream for a frame. In the AdvanceMAME variant a
black & white game additionally sets bit 28 (`COMPLETE_MONOCHROME = 1 << 28`).

### 4.6 `EXIT` — session over

```
cmd = (FLAG_EXIT << 29)            // = 0xE0000000
```

Sent once when closing the connection, to tell the device the game is over.

## 5. Drawing semantics

For each source vector `(xStart, yStart) → (xEnd, yEnd)`:

1. **Clip** the line to the clip window with Cohen–Sutherland. If it lies
   wholly outside, emit nothing.
2. **Convert** both endpoints to device space `0..4095` (§6).
3. If the start does not coincide with the beam's current position, emit a
   **blanked `XY`** (beam off) to the start — this repositions the beam.
4. Emit an `XY` to the end with `blank = (current colour is black)`.

Because step 3 is skipped when the start equals the previous endpoint,
**connected polylines cost only one extra (initial) move** for the whole run.

> **pyvterm deviations from `zvgFrame.c` (both intentional):**
> * `zvgFrame.c` compares the *host-space* start against the stored
>   *device-space* previous point in step 3 — a latent bug that almost always
>   forces a redundant zero-length move. pyvterm compares in device space, so
>   continued polylines really do skip the move.
> * `zvgFrame.c` clamps only the start point to `0..4095`. pyvterm clamps both
>   endpoints. With a clip window inside the bounds this never changes output.

## 6. Coordinate system

The host coordinate space (matching `zvgFrame.h`) is **X ∈ [−512, 511],
Y ∈ [−384, 383]** (a 1024×768 area centred on the origin — MAME's vector
resolution). It maps onto the device's 12-bit `0..4095` grid:

```c
#define X_MIN (-512)   #define X_MAX 511
#define Y_MIN (-384)   #define Y_MAX 383
CONVX(x) = ((x - X_MIN) * 4095) / (X_MAX - X_MIN)   // ((x + 512) * 4095) / 1023
CONVY(y) = ((y - Y_MIN) * 4095) / (Y_MAX - Y_MIN)   // ((y + 384) * 4095) /  767
```

So `(-512, -384) → (0, 0)`, `(511, 383) → (4095, 4095)`, and the origin
`(0, 0) → (2049, 2050)`.

## 7. Device command channel (AdvanceMAME only)

AdvanceMAME can query the device before drawing:

```c
cmd = (FLAG_CMD << 29) | FLAG_CMD_GET_DVG_INFO | (version << 8);   // FLAG_CMD_GET_DVG_INFO = 1
serial_write(&cmd, 4);
serial_read(&cmd, 4);              // echoed command
serial_read(&json_length, 4);     // length of a JSON capability blob
serial_read(json_buf, json_length);
```

It also begins each session with a 512-byte sync preamble
(`0xC0 | (i & 0x3)` repeated). The PiTrex `zvgFrame.c` variant does **not**
use the command channel, the preamble, `FRAME`, or `QUALITY`; `pyvterm`
implements the PiTrex variant, which is what the PiTrex *vecterm* receiver
expects.

## 8. Frame structure on the wire

A complete frame as produced by `zvgFrame.c`'s `serial_send` (and by
`pyvterm.FrameBuilder.to_bytes`):

```
[ FRAME    | total_vector_length ]      <- 4 bytes, written first
[ RGB ...  ]                            ┐
[ XY  ...  ]  (blanked move to a start) │  body: colours and vectors,
[ XY  ...  ]  (lit draw to an end)      │  in the order they were added
   ...                                  ┘
[ QUALITY  | 5 ]
[ COMPLETE ]                            <- 0x00000000
```

The buffer is written to the port in chunks of up to 1024 bytes.

### Worked example

A single white line from host `(0, 0)` to `(100, 0)`:

| Word | Bytes (hex) | Meaning |
| --- | --- | --- |
| `FRAME`    | `80 00 01 90` | total length = 400 |
| `RGB`      | `20 F0 F0 F0` | white (15→240 per channel) |
| `XY` blank | `52 00 48 02` | move to device `(2049, 2050)` |
| `XY` draw  | `42 64 48 02` | draw to device `(2449, 2050)` |
| `QUALITY`  | `60 00 00 05` | quality 5 |
| `COMPLETE` | `00 00 00 00` | end of frame |

This is exactly the byte sequence asserted in `tests/test_frame.py`.

## 9. The reference C sender API

For mapping to `pyvterm`, the sender surface in `zvgFrame.h` is:

| C function | Purpose | pyvterm |
| --- | --- | --- |
| `zvgFrameOpen()` | open + init the serial port | `VectorTerminal.open` / constructor |
| `zvgFrameSetRGB15(r, g, b)` | set colour | `.set_rgb` / `.set_intensity` |
| `zvgFrameSetClipWin(x0, y0, x1, y1)` | set clip window | `.set_clip_window` |
| `zvgFrameVector(x0, y0, x1, y1)` | add a vector | `.vector` |
| `zvgFrameSend()` | serialise + transmit the frame | `.send_frame` |
| `zvgFrameClose()` | send `EXIT`, close the port | `.close` |

## References

* PiTrex sender — `VMMenu/Win32/dvg/zvgFrame.c`, `zvgFrame.h`
  <https://github.com/gtoal/pitrex>
* AdvanceMAME canonical sender — `advance/osd/dvg.c`
  <https://github.com/amadvance/advancemame/blob/master/advance/osd/dvg.c>
* PiTrex Vectrex interface — `pitrex/vectrex/vectrexInterface.h`
* USB-DVG project / community — Mario Montminy
