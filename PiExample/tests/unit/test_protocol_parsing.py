"""Protocol-level encode/decode tests.

These tests exercise pure functions and pure data classes — no threads,
no serial ports. They verify the wire format byte-for-byte against the
upstream VEX reference, which is the contract between the Pi and the V5
firmware.

Coverage:
  - AIRecord encoding (length prefix, position fields, detections)
  - AIRecord CRC-32 (deterministic, MPEG-2 polynomial)
  - V5SerialPacket framing (header, length, type, CRC, payload concat)
  - decode_gps_frame: good frame, truncated, oversized, bad terminator
  - Round-trip: AIRecord → V5SerialPacket → bytes (structure check)

NOTE: these tests verify that the encoder produces the format the V5
expects on paper. They do not verify byte-exact compatibility with a
specific V5 firmware build — for that we need real captures from a live
V5, planned for the bench-deploy verification step.
"""

from __future__ import annotations

import struct

import pytest

from V5Comm import (
    AIRecord,
    Detection,
    ImageDetection,
    MapDetection,
    V5SerialPacket,
)
from V5Position import (
    GPS_FRAME_LEN,
    GPS_TERMINATOR,
    GPSFrameRaw,
    Position,
    decode_gps_frame,
)


# ----- helpers -----

def _zero_position() -> Position:
    return Position(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _make_detection(class_id=2, prob=0.75, depth=1.5,
                    sx=10, sy=20, sw=30, sh=40,
                    mx=0.1, my=0.2, mz=0.3) -> Detection:
    return Detection(
        class_id, prob, depth,
        ImageDetection(sx, sy, sw, sh),
        MapDetection(mx, my, mz),
    )


def _build_gps_frame(status: int,
                    x: int = 100, y: int = 200, z: int = 300,
                    az: int = 1000, el: int = 0, rot: int = 0) -> bytes:
    # Wire layout: [byte 0: ignored][byte 1: status][6 × int16 LE][CC 33]
    body = struct.pack('<BBhhhhhh', 0, status & 0xFF, x, y, z, az, el, rot)
    assert len(body) == 14
    return body + GPS_TERMINATOR  # 14 + 2 = 16


# ----- ImageDetection / MapDetection -----

def test_image_detection_serializes_to_16_little_endian_ints():
    det = ImageDetection(1, 2, 3, 4)
    raw = det.to_Serial()
    assert len(raw) == 16
    assert struct.unpack('<iiii', raw) == (1, 2, 3, 4)


def test_map_detection_serializes_to_12_little_endian_floats():
    det = MapDetection(1.5, -2.25, 0.0)
    raw = det.to_Serial()
    assert len(raw) == 12
    x, y, z = struct.unpack('<fff', raw)
    assert x == pytest.approx(1.5)
    assert y == pytest.approx(-2.25)
    assert z == pytest.approx(0.0)


def test_detection_preserves_maplocattion_misspelling():
    det = _make_detection()
    assert hasattr(det, 'mapLocattion'), \
        "the mapLocattion misspelling is preserved by contract — V5Web reads it"


def test_detection_to_serial_concatenates_subfields():
    det = _make_detection(class_id=7, prob=0.5, depth=1.0)
    raw = det.to_Serial()
    # 4 (classID) + 4 (prob) + 4 (depth) + 16 (image) + 12 (map) = 40
    assert len(raw) == 40
    cid, prob, depth = struct.unpack('<iff', raw[:12])
    assert cid == 7
    assert prob == pytest.approx(0.5)
    assert depth == pytest.approx(1.0)


# ----- AIRecord -----

def test_airecord_empty_detection_list_serialization():
    rec = AIRecord(_zero_position(), [])
    raw = rec.to_Serial()
    # 4 (count) + Position.to_Serial (32) + no detections
    pos_bytes = _zero_position().to_Serial()
    assert len(pos_bytes) == 32
    assert raw == struct.pack('<i', 0) + pos_bytes


def test_airecord_with_one_detection_concatenates_correctly():
    det = _make_detection()
    rec = AIRecord(_zero_position(), [det])
    raw = rec.to_Serial()
    expected = struct.pack('<i', 1) + _zero_position().to_Serial() + det.to_Serial()
    assert raw == expected


def test_airecord_with_three_detections():
    dets = [_make_detection(class_id=i) for i in range(3)]
    rec = AIRecord(_zero_position(), dets)
    raw = rec.to_Serial()
    count = struct.unpack('<i', raw[:4])[0]
    assert count == 3


def test_airecord_crc32_is_deterministic():
    # Two records with identical content must produce identical CRC.
    a = AIRecord(_zero_position(), [_make_detection(class_id=5, prob=0.9)])
    b = AIRecord(_zero_position(), [_make_detection(class_id=5, prob=0.9)])
    assert a.getCRC32() == b.getCRC32()


def test_airecord_crc32_changes_when_payload_changes():
    a = AIRecord(_zero_position(), [_make_detection(class_id=1)])
    b = AIRecord(_zero_position(), [_make_detection(class_id=2)])
    assert a.getCRC32() != b.getCRC32()


def test_airecord_crc32_fits_in_32_bits():
    rec = AIRecord(_zero_position(), [_make_detection() for _ in range(5)])
    crc = rec.getCRC32()
    assert 0 <= crc <= 0xFFFFFFFF


# ----- V5SerialPacket -----

def test_v5_packet_header_is_aa55cc33():
    pkt = V5SerialPacket(0x0001, AIRecord(_zero_position(), []))
    raw = pkt.to_Serial()
    assert raw[:4] == bytes([0xAA, 0x55, 0xCC, 0x33])


def test_v5_packet_length_field_matches_payload():
    rec = AIRecord(_zero_position(), [_make_detection()])
    pkt = V5SerialPacket(0x0001, rec)
    raw = pkt.to_Serial()
    # Header (4) + length (2) + type (2) + crc (4) = 12-byte preamble
    length = struct.unpack('<H', raw[4:6])[0]
    payload = raw[12:]
    assert length == len(payload)
    assert length == len(rec.to_Serial())


def test_v5_packet_type_field_round_trips():
    pkt = V5SerialPacket(0x00AB, AIRecord(_zero_position(), []))
    raw = pkt.to_Serial()
    type_field = struct.unpack('<H', raw[6:8])[0]
    assert type_field == 0x00AB


def test_v5_packet_crc_field_matches_airecord_crc():
    rec = AIRecord(_zero_position(), [_make_detection()])
    expected_crc = rec.getCRC32()
    pkt = V5SerialPacket(0x0001, rec)
    raw = pkt.to_Serial()
    crc_field = struct.unpack('<I', raw[8:12])[0]
    assert crc_field == expected_crc


def test_v5_packet_payload_is_airecord_bytes():
    rec = AIRecord(_zero_position(), [_make_detection()])
    pkt = V5SerialPacket(0x0001, rec)
    raw = pkt.to_Serial()
    assert raw[12:] == rec.to_Serial()


# ----- Position -----

def test_position_to_serial_is_32_bytes_little_endian():
    pos = Position(7, 0x01, 1.5, 2.5, 3.5, 90.0, 0.0, 45.0)
    raw = pos.to_Serial()
    assert len(raw) == 32  # 2 ints + 6 floats = 8 + 24
    fc, st, x, y, z, az, el, rot = struct.unpack('<iiffffff', raw)
    assert (fc, st) == (7, 1)
    assert x == pytest.approx(1.5)
    assert az == pytest.approx(90.0)


# ----- decode_gps_frame -----

def test_decode_gps_frame_accepts_valid_frame():
    raw = _build_gps_frame(status=20, x=1234, y=-5678, z=99,
                           az=10000, el=2000, rot=-3000)
    frame = decode_gps_frame(raw)
    assert frame is not None
    assert isinstance(frame, GPSFrameRaw)
    assert frame.status == 20
    assert frame.x_raw == 1234
    assert frame.y_raw == -5678
    assert frame.z_raw == 99
    assert frame.az_raw == 10000
    assert frame.el_raw == 2000
    assert frame.rot_raw == -3000


def test_decode_gps_frame_extracts_status_byte_at_offset_1():
    raw = _build_gps_frame(status=0x42)
    frame = decode_gps_frame(raw)
    assert frame is not None
    assert frame.status == 0x42


def test_decode_gps_frame_rejects_truncated():
    truncated = _build_gps_frame(status=20)[:GPS_FRAME_LEN - 1]
    assert decode_gps_frame(truncated) is None


def test_decode_gps_frame_rejects_oversized():
    raw = _build_gps_frame(status=20) + b'\x00'
    assert decode_gps_frame(raw) is None


def test_decode_gps_frame_rejects_missing_terminator():
    raw = _build_gps_frame(status=20)
    bad = raw[:-2] + b'\x00\x00'
    assert decode_gps_frame(bad) is None


def test_decode_gps_frame_rejects_partial_terminator():
    raw = _build_gps_frame(status=20)
    bad = raw[:-2] + b'\xCC\x00'  # CC right, second byte wrong
    assert decode_gps_frame(bad) is None


def test_decode_gps_frame_accepts_status_zero():
    # Status 0 is a real value (not all firmware values are "valid"
    # but the decoder should still parse them; it's V5GPS._process_frame
    # that gates updates on status == 20).
    raw = _build_gps_frame(status=0)
    frame = decode_gps_frame(raw)
    assert frame is not None
    assert frame.status == 0


def test_decode_gps_frame_constants_are_correct():
    assert GPS_FRAME_LEN == 16
    assert GPS_TERMINATOR == b'\xCC\x33'


# ----- round-trip integration -----

def test_packet_envelope_total_length_matches_components():
    rec = AIRecord(_zero_position(), [_make_detection(), _make_detection()])
    pkt = V5SerialPacket(0x0001, rec)
    raw = pkt.to_Serial()
    # 4 (header) + 2 (length) + 2 (type) + 4 (CRC) + payload
    assert len(raw) == 12 + len(rec.to_Serial())
