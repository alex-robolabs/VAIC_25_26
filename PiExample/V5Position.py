"""Pi <-> V5 GPS sensor link.

Wire protocol (unchanged from VEX reference; preserved byte-for-byte):
  - V5 GPS streams 16-byte binary frames terminated by 0xCC 0x33.
  - Layout (little-endian):
        [byte 0: ignored]
        [byte 1: status u8]
        [bytes 2..13: x,y,z,azimuth,elevation,rotation as i16 each]
        [bytes 14..15: 0xCC 0x33 terminator]
  - x/y/z are in units of 0.1 mm; angles in units of 180/32768 degrees.

`Position` and `decode_gps_frame` are pure data and pure decoder
respectively — reusable from tests without instantiating a link. The
connection manager (`V5GPS`) subclasses `SerialLink`.

Public API:
    gps = V5GPS()
    gps.start()
    pos = gps.getPosition()          # defensive copy
    gps.isConnected()                # alias for is_healthy()
    gps.updateOffset(gpsOffset)      # called from V5Web on calibration change
    gps.stop()
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from threading import Lock
from typing import Optional

import serial

from filter import LiveFilter
from serial_link import SerialLink


class Position:
    """GPS / localization state sent from V5 to Pi, forwarded to V5Web
    for the dashboard. Plain mutable object preserved for backwards
    compatibility with consumers that read individual fields."""

    STATUS_CONNECTED  = 0x00000001
    STATUS_NODOTS     = 0x00000002
    STATUS_NORAWBITS  = 0x00000004
    STATUS_NOGROUPS   = 0x00000008
    STATUS_NOBITS     = 0x00000010
    STATUS_PIXELERROR = 0x00000020
    STATUS_SOLVER     = 0x00000040
    STATUS_ANGLEJUMP  = 0x00000080
    STATUS_POSJUMP    = 0x00000100
    STATUS_NOSOLUTION = 0x00000200
    STATUS_KALMAN_EST = 0x00100000

    def __init__(self, frameCount, status, x, y, z, azimuth, elevation, rotation):
        self.frameCount = frameCount
        self.status = status
        self.x = x
        self.y = y
        self.z = z
        self.azimuth = azimuth
        self.elevation = elevation
        self.rotation = rotation

    def to_Serial(self):
        return struct.pack('<iiffffff',
                           self.frameCount, self.status,
                           self.x, self.y, self.z,
                           self.azimuth, self.elevation, self.rotation)

    def to_JSON(self):
        return {
            'frameCount': self.frameCount,
            'status': self.status,
            'x': self.x,
            'y': self.y,
            'z': self.z,
            'azimuth': self.azimuth,
            'elevation': self.elevation,
            'rotation': self.rotation,
        }


@dataclass
class GPSFrameRaw:
    """Decoded raw fields from a single 16-byte GPS frame, before unit
    conversion or offset application. `decode_gps_frame` returns this;
    `V5GPS._process_frame` is the only consumer in production."""
    status: int
    x_raw: int
    y_raw: int
    z_raw: int
    az_raw: int
    el_raw: int
    rot_raw: int


GPS_FRAME_LEN = 16
GPS_TERMINATOR = b'\xCC\x33'


def decode_gps_frame(data: bytes) -> Optional[GPSFrameRaw]:
    """Pure decoder. Returns None if the frame is malformed (wrong
    length or missing terminator). Returns raw integer fields otherwise;
    callers apply unit conversion and offsets.
    """
    if len(data) != GPS_FRAME_LEN:
        return None
    if data[-2:] != GPS_TERMINATOR:
        return None
    status = data[1]
    x, y, z, az, el, rot = struct.unpack('<hhhhhh', data[2:14])
    return GPSFrameRaw(status, x, y, z, az, el, rot)


class V5GPS(SerialLink):
    """Self-healing V5 GPS sensor link."""

    def __init__(self, port: Optional[str] = None):
        super().__init__(
            name="v5-gps",
            port_filter=lambda d: "GPS" in d.description and "User" in d.description,
            baudrate=115200,
            read_timeout=1.0,
            max_timeouts=5,
            explicit_port=port,
        )
        self._position = Position(0, 0, 0, 0, 0, 0, 0, 0)
        self._position_lock = Lock()
        self._frame_count = 0
        self._heading_offset = 0
        self._gps_x_offset = 0
        self._gps_y_offset = 0
        self._offset_units = "meters"
        self._filter = LiveFilter(10)

    # ---------- public API (preserved) ----------

    def isConnected(self) -> bool:
        return self.is_healthy()

    def getPosition(self) -> Position:
        with self._position_lock:
            return Position(
                self._position.frameCount,
                self._position.status,
                self._position.x,
                self._position.y,
                self._position.z,
                self._position.azimuth,
                self._position.elevation,
                self._position.rotation,
            )

    def updateOffset(self, newOffset) -> None:
        unitDivisor = 1
        if newOffset.unit in ("CM", "cm"):
            unitDivisor = 100
        elif newOffset.unit in ("MM", "mm"):
            unitDivisor = 1000
        elif newOffset.unit in ("IN", "in", "inches"):
            unitDivisor = 39.3701
        elif newOffset.unit not in ("m", "meters", "M"):
            raise Exception("Invalid argument: Unit not accepted")
        self._heading_offset = newOffset.heading_offset
        self._gps_x_offset = newOffset.x / unitDivisor
        self._gps_y_offset = newOffset.y / unitDivisor
        self._offset_units = "meters"

    # ---------- SerialLink override ----------

    def _handle_session(self, ser: serial.Serial) -> None:
        self._frame_count = 0
        try:
            while not self._stop_event.is_set():
                data = ser.read_until(GPS_TERMINATOR)
                self._record_read(data)
                if not data:
                    continue
                self._process_frame(data)
        finally:
            # Mark disconnected when leaving the session so consumers
            # (V5Web, pushback) see a clean "GPS down" state immediately.
            with self._position_lock:
                self._position.status = 0

    def _process_frame(self, data: bytes) -> None:
        raw = decode_gps_frame(data)
        if raw is None:
            self._record_parse_error(
                ValueError(f"invalid gps frame, len={len(data)}"))
            return
        self._frame_count += 1
        self._record_packet_in()

        x = raw.x_raw / 10000.0
        y = raw.y_raw / 10000.0
        z = raw.z_raw / 10000.0
        az = ((raw.az_raw / 32768.0 * 180.0) - self._heading_offset) % 360
        el = raw.el_raw / 32768.0 * 180.0
        rot = raw.rot_raw / 32768.0 * 180.0

        # Rotate the stored GPS offset by current heading so it cancels
        # the GPS sensor's offset from robot origin correctly.
        theta = math.radians(az)
        new_x_offset = (self._gps_x_offset * math.cos(theta)
                        + self._gps_y_offset * math.sin(theta))
        new_y_offset = (-self._gps_x_offset * math.sin(theta)
                        + self._gps_y_offset * math.cos(theta))
        x -= new_x_offset
        y -= new_y_offset

        local_status = Position.STATUS_CONNECTED
        if 0 < raw.status < 32:
            local_status |= (1 << raw.status)

        # status == 20 is the "valid solution" code from upstream firmware.
        if raw.status == 20:
            x, y = self._filter.update(x, y)
            with self._position_lock:
                self._position.x = x
                self._position.y = y
                self._position.z = z
                self._position.azimuth = az
                self._position.elevation = el
                self._position.rotation = rot
                self._position.status = local_status
                self._position.frameCount = self._frame_count
