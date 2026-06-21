"""Wire-format tests — these pin the exact bytes the protocol emits."""

from pyvterm import protocol
from pyvterm.protocol import Bounds, Flag


def test_flag_values():
    assert Flag.COMPLETE == 0x0
    assert Flag.RGB == 0x1
    assert Flag.XY == 0x2
    assert Flag.QUALITY == 0x3
    assert Flag.FRAME == 0x4
    assert Flag.CMD == 0x5
    assert Flag.EXIT == 0x7


def test_pack_word_is_big_endian():
    assert protocol.pack_word(0x80000190) == b"\x80\x00\x01\x90"
    assert protocol.pack_word(0x1_00000000) == b"\x00\x00\x00\x00"  # masked to 32 bits


def test_scale_color():
    assert protocol.scale_color(0) == 0
    assert protocol.scale_color(8) == 128
    assert protocol.scale_color(15) == 240
    assert protocol.scale_color(16) == 255  # saturates
    assert protocol.scale_color(255) == 255


def test_encode_rgb_word():
    assert protocol.encode_rgb_word(0xF0, 0xF0, 0xF0) == 0x20F0F0F0
    assert protocol.encode_rgb_word(0x12, 0x34, 0x56) == 0x20123456


def test_encode_xy_word():
    # Blanked move to (2049, 2050).
    assert protocol.encode_xy_word(2049, 2050, True) == 0x52004802
    # Lit draw to (2449, 2050).
    assert protocol.encode_xy_word(2449, 2050, False) == 0x42644802
    # Coordinates wider than 14 bits are masked.
    assert protocol.encode_xy_word(0xFFFF, 0xFFFF, False) == (
        (Flag.XY << 29) | (0x3FFF << 14) | 0x3FFF
    )


def test_encode_frame_quality_complete_exit():
    assert protocol.encode_frame_word(400) == 0x80000190
    assert protocol.encode_quality_word(5) == 0x60000005
    assert protocol.encode_complete_word() == 0x00000000
    assert protocol.encode_complete_word(monochrome=True) == 0x10000000
    assert protocol.encode_exit_word() == 0xE0000000


def test_bounds_conversion_endpoints():
    b = Bounds()
    assert b.conv_x(-512) == 0
    assert b.conv_x(511) == 4095
    assert b.conv_y(-384) == 0
    assert b.conv_y(383) == 4095
    assert b.conv_x(0) == 2049
    assert b.conv_y(0) == 2050


def test_keepalive_word():
    # CMD flag (5 << 29) | 'K' (0x4B).
    assert protocol.encode_keepalive_word() == 0xA000004B
    assert protocol.keepalive() == protocol.pack_word(0xA000004B)
    # Distinct from the HELLO probe so a receiver can tell them apart.
    assert protocol.encode_keepalive_word() != protocol.encode_hello_word()


def test_byte_wrappers_match_word_encoders():
    assert protocol.rgb(0xF0, 0xF0, 0xF0) == protocol.pack_word(0x20F0F0F0)
    assert protocol.rgb_scaled(15, 15, 15) == protocol.pack_word(0x20F0F0F0)
    assert protocol.xy(2049, 2050, True) == protocol.pack_word(0x52004802)
    assert protocol.frame_header(400) == b"\x80\x00\x01\x90"
    assert protocol.quality(5) == b"\x60\x00\x00\x05"
    assert protocol.complete() == b"\x00\x00\x00\x00"
    assert protocol.exit_command() == b"\xe0\x00\x00\x00"


def test_decode_round_trips():
    assert protocol.decode_word(protocol.encode_xy_word(100, 200, True)) == {
        "flag": Flag.XY,
        "blank": True,
        "x": 100,
        "y": 200,
    }
    assert protocol.decode_word(protocol.encode_rgb_word(0x12, 0x34, 0x56)) == {
        "flag": Flag.RGB,
        "r": 0x12,
        "g": 0x34,
        "b": 0x56,
    }
    assert protocol.decode_word(protocol.encode_frame_word(400)) == {
        "flag": Flag.FRAME,
        "vector_length": 400,
    }
    assert protocol.decode_word(protocol.encode_quality_word(5)) == {
        "flag": Flag.QUALITY,
        "value": 5,
    }
    assert protocol.decode_word(protocol.encode_complete_word(True)) == {
        "flag": Flag.COMPLETE,
        "monochrome": True,
    }
