"""Tests for the v2 extensions: EXT/HELLO encoding, expanders, fallback,
negotiation, and frame suppression.

The golden HEIGHTFIELD/POLYLINE byte sequences here are the cross-language
contract: vekterm's ``tests/test_ext.c`` feeds the identical bytes to its parser
and asserts the same reconstructed segments.
"""

from pyvterm import Capability, MemoryTransport, VectorTerminal, ext, protocol
from pyvterm.protocol import Flag

# --- canonical HEIGHTFIELD (3x2 grid) -------------------------------------

HF_KW = {
    "cols": 3,
    "rows": 2,
    "x0": 1000,
    "x_step": 500,
    "y0": 2000,
    "y_step": -400,
    "y_scale": 256,
    "displacement": bytes([0, 128, 255, 255, 0, 64]),
    "brightness": 240,
}
HF_GOLDEN = bytes.fromhex(
    "c1000016"  # EXT subtype=1 (HEIGHTFIELD) length=22
    "00"  # flags: none
    "0003"  # cols=3
    "0002"  # rows=2
    "03e8"  # x0=1000
    "01f4"  # x_step=500
    "07d0"  # y0=2000
    "fe70"  # y_step=-400
    "0100"  # y_scale=256
    "f0"  # brightness=240
    "0080ff"  # displacement row 0
    "ff0040"  # displacement row 1
)
HF_SEGMENTS = [
    (1000, 2000, 1500, 2128, 240),
    (1500, 2128, 2000, 2255, 240),
    (1000, 1855, 1500, 1600, 240),
    (1500, 1600, 2000, 1664, 240),
]

# --- canonical POLYLINE ----------------------------------------------------

PL_KW = {"x0": 2048, "y0": 2048, "deltas": [(10, 0), (0, 20), (-5, -5)], "brightness": 200}
PL_GOLDEN = bytes.fromhex(
    "c200000e"  # EXT subtype=2 (POLYLINE) length=14
    "00"  # flags: none
    "c8"  # brightness=200
    "0800"  # x0=2048
    "0800"  # y0=2048
    "0004"  # count=4
    "0a00"  # (10, 0)
    "0014"  # (0, 20)
    "fbfb"  # (-5, -5)
)
PL_SEGMENTS = [
    (2048, 2048, 2058, 2048, 200),
    (2058, 2048, 2058, 2068, 200),
    (2058, 2068, 2053, 2063, 200),
]


def test_heightfield_exact_bytes():
    assert ext.encode_heightfield(**HF_KW) == HF_GOLDEN


def test_heightfield_expansion():
    assert ext.expand_heightfield(**HF_KW) == HF_SEGMENTS


def test_heightfield_intensity_plane_blanks_gaps():
    # Intensity 0 breaks the run, so a dark column splits the scan line.
    intensity = bytes([240, 0, 240, 240, 240, 240])
    segs = ext.expand_heightfield(**HF_KW, intensity=intensity)
    # Row 0: c1 is blanked, so the only break leaves no lit pair around it;
    # row 1 stays a single 2-segment run.
    assert segs == [
        # row 0: c0 starts a run, c1 blanks (run reset), c2 starts a fresh run -> no segments
        (1000, 1855, 1500, 1600, 240),
        (1500, 1600, 2000, 1664, 240),
    ]


def test_heightfield_serpentine_reverses_odd_rows():
    segs = ext.expand_heightfield(**HF_KW, serpentine=True)
    # Row 1 is now walked c2,c1,c0 -> endpoints reversed in x.
    assert segs[2] == (2000, 1664, 1500, 1600, 240)
    assert segs[3] == (1500, 1600, 1000, 1855, 240)


def test_polyline_exact_bytes():
    assert ext.encode_polyline(**PL_KW) == PL_GOLDEN


def test_polyline_expansion():
    assert ext.expand_polyline(**PL_KW) == PL_SEGMENTS


def test_polyline_closed_adds_return_segment():
    segs = ext.expand_polyline(**PL_KW, closed=True)
    assert segs[-1] == (2053, 2063, 2048, 2048, 200)


def test_fallback_frame_is_valid_base_protocol():
    # The software fallback must be a well-formed base frame: FRAME ... COMPLETE.
    segs = ext.expand_heightfield(**HF_KW)
    data = ext.segments_to_base_frame(segs)
    decoded = [
        protocol.decode_word(int.from_bytes(data[i : i + 4], "big")) for i in range(0, len(data), 4)
    ]
    assert decoded[0]["flag"] is Flag.FRAME
    assert decoded[-1]["flag"] is Flag.COMPLETE
    lit = [w for w in decoded if w["flag"] is Flag.XY and not w["blank"]]
    assert len(lit) == len(segs)  # one lit XY per reconstructed segment


def test_ext_header_roundtrip():
    data = protocol.encode_ext_header(protocol.ExtSubtype.HEIGHTFIELD, 22)
    word = int.from_bytes(data, "big")
    assert (word >> protocol.FLAG_SHIFT) == Flag.EXT
    assert ((word >> protocol.EXT_SUBTYPE_SHIFT) & 0x1F) == protocol.ExtSubtype.HEIGHTFIELD
    assert (word & protocol.EXT_LENGTH_MASK) == 22


# --- HELLO negotiation -----------------------------------------------------

HELLO_REPLY = bytes.fromhex("564b02070c080bb8200032 00".replace(" ", ""))


def test_hello_word_value():
    assert protocol.encode_hello_word() == 0xA0000056


def test_decode_hello_descriptor():
    d = protocol.decode_hello_descriptor(HELLO_REPLY)
    assert d is not None
    assert d.version == 2
    assert d.capabilities == 0x07
    assert d.coord_bits == 12
    assert d.max_pipeline == 3000
    assert d.max_payload == 8192
    assert d.refresh_hz == 50
    assert d.supports(Capability.HEIGHTFIELD)
    assert d.supports(Capability.POLYLINE)


def test_decode_hello_skips_leading_sync_bytes():
    # A real read may include flow-control 0x06 bytes before the descriptor.
    d = protocol.decode_hello_descriptor(b"\x06\x06" + HELLO_REPLY)
    assert d is not None and d.version == 2


def test_decode_hello_rejects_garbage():
    assert protocol.decode_hello_descriptor(b"not a vekterm") is None


class _FakeSerial:
    """Minimal pyserial stand-in that replies to the HELLO probe."""

    def __init__(self, reply: bytes = HELLO_REPLY, **_: object) -> None:
        self.written = bytearray()
        self._reply = reply
        self.in_waiting = len(reply)

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def read(self, size: int = 1) -> bytes:
        chunk, self._reply = self._reply[:size], self._reply[size:]
        self.in_waiting = len(self._reply)
        return chunk

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def _terminal_with_fake(reply: bytes = HELLO_REPLY) -> VectorTerminal:
    from pyvterm.transport import DEFAULT_BAUDRATE, SerialTransport

    # A fixed baud skips auto-detection (it would consume the one-shot reply);
    # these tests exercise negotiate() explicitly.
    transport = SerialTransport(
        "loop",
        baudrate=DEFAULT_BAUDRATE,
        settle=0,
        serial_factory=lambda **kw: _FakeSerial(reply, **kw),
    )
    return VectorTerminal(transport=transport)


def test_negotiate_detects_vekterm():
    vt = _terminal_with_fake()
    desc = vt.negotiate()
    assert desc is not None and desc.version == 2
    assert vt.supports(Capability.HEIGHTFIELD)
    # The probe word went out on the wire.
    assert vt.transport._serial.written[:4] == protocol.hello_word()  # type: ignore[attr-defined]


def test_negotiate_times_out_on_plain_device():
    vt = _terminal_with_fake(reply=b"")  # device never answers
    vt.transport.sync_timeout = 0.05  # type: ignore[attr-defined]
    assert vt.negotiate() is None
    assert not vt.supports(Capability.HEIGHTFIELD)


def test_send_heightfield_uses_ext_when_capable():
    vt = _terminal_with_fake()
    vt.negotiate()
    data = vt.send_heightfield(**HF_KW)
    # Wrapped as FRAME + EXT(HEIGHTFIELD) + COMPLETE.
    assert data[4:-4] == HF_GOLDEN
    assert data[:4] == protocol.frame_header(0)


def test_send_heightfield_falls_back_without_negotiation():
    vt = VectorTerminal(transport=MemoryTransport())  # no capabilities
    data = vt.send_heightfield(**HF_KW)
    # Base frame: no EXT word present.
    words = [int.from_bytes(data[i : i + 4], "big") for i in range(0, len(data), 4)]
    assert all((w >> protocol.FLAG_SHIFT) != Flag.EXT for w in words)
    lit = [protocol.decode_word(w) for w in words]
    assert sum(1 for w in lit if w["flag"] is Flag.XY and not w["blank"]) == len(HF_SEGMENTS)


# --- frame suppression -----------------------------------------------------


def test_frame_suppression_skips_identical_frames():
    transport = MemoryTransport()
    vt = VectorTerminal(transport=transport, suppress_duplicates=True)
    for _ in range(3):
        vt.set_intensity(15)
        vt.move_to(0, 0)
        vt.draw_to(100, 0)
        vt.send_frame()
        vt.clear()
    assert len(transport.frames) == 1  # only the first was transmitted
    assert vt.frames_suppressed == 2


def test_frame_suppression_sends_when_changed():
    transport = MemoryTransport()
    vt = VectorTerminal(transport=transport, suppress_duplicates=True)
    vt.set_intensity(15)
    vt.draw_to(100, 0)
    vt.send_frame()
    vt.clear()
    vt.set_intensity(15)
    vt.draw_to(50, 50)  # different geometry
    vt.send_frame()
    assert len(transport.frames) == 2
    assert vt.frames_suppressed == 0
