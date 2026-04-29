"""Pi <-> V5 Brain detection-data link.

Wire protocol (unchanged from VEX reference; preserved byte-for-byte):
  - V5 polls with the ASCII line "AA55CC3301\\n"
  - Pi responds with one binary V5SerialPacket:
        [0xAA 0x55 0xCC 0x33][length:u16][type:u16][crc32:u32][payload]
  - Repeats at the V5's poll rate (~15 Hz).

The protocol classes (`ImageDetection`, `MapDetection`, `Detection`,
`AIRecord`, `V5SerialPacket`) are preserved verbatim from the upstream
VEX implementation — they are correct and must stay paired with the V5
firmware that consumes them.

The connection manager (`V5SerialComms`) subclasses `SerialLink` for
self-healing reconnection and structured state tracking. Public API:

    v5 = V5SerialComms()
    v5.start()
    v5.setDetectionData(aiRecord)
    v5.is_healthy()                # OPERATING semantics — see SerialLink
    v5.state()                     # five-state enum
    v5.stats()                     # snapshot of counters/gauges
    v5.stop()

Or as a context manager:

    with V5SerialComms() as v5:
        v5.setDetectionData(...)
"""

from __future__ import annotations

import struct
from threading import Lock
from typing import Optional

import serial

from V5Position import Position
from serial_link import SerialLink


class ImageDetection:
    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height

    def to_Serial(self):
        return struct.pack('<iiii', self.x, self.y, self.width, self.height)

    def to_JSON(self):
        return self.__dict__


class MapDetection:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z

    def to_Serial(self):
        return struct.pack('<fff', self.x, self.y, self.z)

    def to_JSON(self):
        return self.__dict__


class Detection:
    def __init__(self, classID, probability, depth, screenLocation, mapLocation):
        self.classID = classID
        self.probability = probability
        self.depth = depth
        self.screenLocation = screenLocation
        # Misspelling "mapLocattion" preserved — V5Web.py reads this field
        # by name; renaming would break the dashboard data model.
        self.mapLocattion = mapLocation

    def to_Serial(self):
        data = struct.pack('<iff', self.classID, self.probability, self.depth)
        data += self.screenLocation.to_Serial()
        data += self.mapLocattion.to_Serial()
        return data

    def to_JSON(self):
        outData = {}
        outData['class'] = self.classID
        outData['prob'] = self.probability
        outData['depth'] = self.depth
        outData['screenLocation'] = self.screenLocation.to_JSON()
        outData['mapLocation'] = self.mapLocattion.to_JSON()
        return outData


class AIRecord:
    """The record transmitted from Pi to V5 Brain per poll."""

    POLYNOMIAL_CRC32 = 0x04C11DB7  # MPEG-2 variant (no reflect, no xor-out)
    __crc32_table = [0] * 256
    __table32Generated = 0

    def __init__(self, position, detections):
        self.position = position
        self.detections = detections

    def to_Serial(self):
        data = struct.pack('<i', len(self.detections))
        data += self.position.to_Serial()
        for det in self.detections:
            data += det.to_Serial()
        return data

    def to_JSON(self):
        outData = {}
        outData['position'] = self.position.to_JSON()
        outData['detections'] = [det.to_JSON() for det in self.detections]
        return outData

    def __Crc32GenerateTable(self):
        for i in range(256):
            crc_accum = i << 24
            for j in range(8):
                if crc_accum & 0x80000000:
                    crc_accum = (crc_accum << 1) ^ AIRecord.POLYNOMIAL_CRC32
                else:
                    crc_accum = crc_accum << 1
            AIRecord.__crc32_table[i] = crc_accum
        AIRecord.__table32Generated = 1

    def __Crc32Generate(self, data, accumulator):
        if not AIRecord.__table32Generated:
            self.__Crc32GenerateTable()
        for j in range(len(data)):
            i = ((accumulator >> 24) ^ data[j]) & 0xFF
            accumulator = (accumulator << 8) ^ AIRecord.__crc32_table[i]
        return accumulator

    def getCRC32(self):
        data = self.to_Serial()
        crc = self.__Crc32Generate(data, 0) & 0xFFFFFFFF
        return crc


class V5SerialPacket:
    HEADER = bytes([0xAA, 0x55, 0xCC, 0x33])

    def __init__(self, type, detections):
        self.__length = len(detections.to_Serial())
        self.__type = type
        self.__detections = detections

    def to_Serial(self):
        data = bytearray(self.HEADER)
        data += struct.pack('<HHI',
                            self.__length, self.__type,
                            self.__detections.getCRC32())
        data += self.__detections.to_Serial()
        return data


class V5SerialComms(SerialLink):
    """Self-healing V5 detection-data link."""

    _MAP_PACKET_TYPE = 0x0001
    _HANDSHAKE = "AA55CC3301"

    def __init__(self, port: Optional[str] = None):
        super().__init__(
            name="v5-data",
            # Two-substring AND disambiguates V5 Brain User Port from
            # GPS Sensor User Port (both descriptors contain "User").
            # Don't simplify to one substring.
            port_filter=lambda d: "V5" in d.description and "User" in d.description,
            baudrate=115200,
            read_timeout=1.0,
            max_timeouts=5,
            explicit_port=port,
        )
        self._detections = AIRecord(Position(0, 0, 0, 0, 0, 0, 0, 0), [])
        self._detection_lock = Lock()

    # ---------- public API (preserved) ----------

    def setDetectionData(self, data) -> None:
        """Update the payload that will be written on the next poll."""
        with self._detection_lock:
            self._detections = data

    def isConnected(self) -> bool:
        """Backwards-compatible alias preserved for nav code that
        predates `is_healthy()`. Same semantics."""
        return self.is_healthy()

    # ---------- SerialLink override ----------

    def _handle_session(self, ser: serial.Serial) -> None:
        while not self._stop_event.is_set():
            line = ser.readline()
            self._record_read(line)
            if not line:
                continue
            # errors="replace" defends against stray non-ASCII bytes
            # without killing the thread; we just won't match the
            # handshake on garbage.
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded == self._HANDSHAKE:
                with self._detection_lock:
                    packet_bytes = V5SerialPacket(
                        self._MAP_PACKET_TYPE, self._detections).to_Serial()
                self._record_packet_in()
                n = ser.write(packet_bytes)
                self._record_write(n)
            elif decoded:
                self._log.debug("unexpected line on data port: %r", decoded)
