"""Jetson <-> V5 GPS sensor link.

Wire protocol (unchanged from original VEX reference implementation):
  - V5 streams 16-byte binary frames, each terminated by 0xCC 0x33
  - Fields (little-endian):
        [1 byte][status:u8][x:i16][y:i16][z:i16][az:i16][el:i16][rot:i16][0xCC][0x33]
  - x/y/z are in units of 0.1 mm; angles in units of 180/32768 degrees.

Position class and offset logic preserved verbatim. V5GPS now subclasses
SerialLink for self-healing reconnection, watchdog, and logging.

Public API is unchanged:
    gps = V5GPS()
    gps.start()
    pos = gps.getPosition()          # Position object
    gps.isConnected()                # True iff data is fresh
    gps.updateOffset(gpsOffset)      # from V5Web
    gps.stop()
"""

import math
import struct
from threading import Lock

from filter import LiveFilter
from serial_link import SerialLink


class Position:
    """GPS / localization state sent from V5 to Jetson, and forwarded
    to V5Web for the dashboard. Kept as a plain mutable object for
    backwards compatibility with consumers that read individual fields."""

    STATUS_CONNECTED    = 0x00000001
    STATUS_NODOTS       = 0x00000002
    STATUS_NORAWBITS    = 0x00000004
    STATUS_NOGROUPS     = 0x00000008
    STATUS_NOBITS       = 0x00000010
    STATUS_PIXELERROR   = 0x00000020
    STATUS_SOLVER       = 0x00000040
    STATUS_ANGLEJUMP    = 0x00000080
    STATUS_POSJUMP      = 0x00000100
    STATUS_NOSOLUTION   = 0x00000200
    STATUS_KALMAN_EST   = 0x00100000

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
        outData = {}
        outData['frameCount'] = self.frameCount
        outData['status'] = self.status
        outData['x'] = self.x
        outData['y'] = self.y
        outData['z'] = self.z
        outData['azimuth'] = self.azimuth
        outData['elevation'] = self.elevation
        outData['rotation'] = self.rotation
        return outData


class V5GPS(SerialLink):
    """Self-healing V5 GPS link."""

    def __init__(self, port=None):
        super().__init__(
            name="v5-gps",
            port_filter=lambda d: "GPS" in d.description and "User" in d.description,
            baudrate=115200,
            read_timeout=1.0,
            watchdog_seconds=5.0,
        )
        self._explicit_port = port
        self._position = Position(0, 0, 0, 0, 0, 0, 0, 0)
        self._position_lock = Lock()
        self._frame_count = 0
        self._heading_offset = 0
        self._gps_x_offset = 0
        self._gps_y_offset = 0
        self._offset_units = "meters"
        self._filter = LiveFilter(10)

    # ---------- public API (preserved) ----------

    def isConnected(self):
        """Alias for is_healthy(). Preserves old API so V5Web and
        pushback.py don't need to know about the new method name."""
        return self.is_healthy()

    def getPosition(self):
        """Defensive copy so callers can't accidentally mutate our state."""
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

    def updateOffset(self, newOffset):
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

    # ---------- SerialLink overrides ----------

    def _find_port(self):
        if self._explicit_port is not None:
            return self._explicit_port
        return super()._find_port()

    def _handle_session(self, ser):
        self._frame_count = 0
        try:
            while not self._stop_event.is_set():
                data = ser.read_until(b'\xCC\x33')
                if not data:
                    # read_timeout fired with no data; loop back
                    continue
                self._record_rx(len(data))
                if len(data) != 16:
                    self._log.debug(
                        "frame size mismatch: %d bytes (expected 16)", len(data))
                    continue
                try:
                    self._process_frame(data)
                except struct.error as e:
                    self._log.warning("frame decode error: %s", e)
        finally:
            # Mark disconnected when leaving the session so consumers
            # (V5Web, pushback) see a clean "GPS down" state immediately.
            with self._position_lock:
                self._position.status = 0

    def _process_frame(self, data):
        self._frame_count += 1
        status = data[1]
        x_raw, y_raw, z_raw, az_raw, el_raw, rot_raw = struct.unpack(
            '<hhhhhh', data[2:14])

        x = x_raw / 10000.0
        y = y_raw / 10000.0
        z = z_raw / 10000.0
        az = ((az_raw / 32768.0 * 180.0) - self._heading_offset) % 360
        el = el_raw / 32768.0 * 180.0
        rot = rot_raw / 32768.0 * 180.0

        # Rotate the stored GPS offset by current heading so it cancels
        # the offset of the GPS sensor from robot origin correctly.
        theta = math.radians(az)
        new_x_offset = (self._gps_x_offset * math.cos(theta)
                        + self._gps_y_offset * math.sin(theta))
        new_y_offset = (-self._gps_x_offset * math.sin(theta)
                        + self._gps_y_offset * math.cos(theta))
        x -= new_x_offset
        y -= new_y_offset

        local_status = Position.STATUS_CONNECTED
        if 0 < status < 32:
            local_status |= (1 << status)

        # status == 20 is the "valid solution" code in the original firmware.
        if status == 20:
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
