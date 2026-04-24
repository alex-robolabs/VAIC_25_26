"""Jetson <-> V5 Brain data link.

Wire protocol (unchanged from original VEX reference implementation):
  - V5 polls with the ASCII line "AA55CC3301\\n"
  - Jetson responds with one binary V5SerialPacket:
        [0xAA 0x55 0xCC 0x33][length:u16][type:u16][crc32:u32][payload]
  - Repeats at the V5's poll rate.

The protocol classes (ImageDetection, MapDetection, Detection, AIRecord,
V5SerialPacket) are preserved verbatim — they are correct. The connection
manager (V5SerialComms) now subclasses SerialLink for self-healing
reconnection, watchdog, and structured logging.

Public API is unchanged:
    v5 = V5SerialComms()
    v5.start()
    v5.setDetectionData(aiRecord)
    v5.stop()
    v5.is_healthy()    # NEW — use this for dashboard health indicators
"""

import struct
import threading
from threading import Lock

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
        # Note: misspelling "mapLocattion" preserved — V5Web.py reads this field by name.
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
    """The record transmitted from Jetson to V5 Brain per poll."""

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

    # CRC-32 (MPEG-2 variant, no reflect, no xor-out). Unchanged.
    POLYNOMIAL_CRC32 = 0x04C11DB7
    __crc32_table = [0] * 256
    __table32Generated = 0

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
    def __init__(self, type, detections):
        self.__length = len(detections.to_Serial())
        self.__type = type
        self.__detections = detections

    def to_Serial(self):
        data = bytearray([0xAA, 0x55, 0xCC, 0x33])
        data += struct.pack('<HHI',
                            self.__length, self.__type,
                            self.__detections.getCRC32())
        data += self.__detections.to_Serial()
        return data


class V5SerialComms(SerialLink):
    """Self-healing V5 data link. See module docstring for protocol."""

    _MAP_PACKET_TYPE = 0x0001
    _HANDSHAKE = "AA55CC3301"

    def __init__(self, port=None):
        # `port` preserved for backwards compatibility; None means auto-discover.
        super().__init__(
            name="v5-data",
            port_filter=lambda d: "V5" in d.description and "User" in d.description,
            baudrate=115200,
            read_timeout=1.0,
            watchdog_seconds=5.0,
        )
        self._explicit_port = port
        self._detections = AIRecord(Position(0, 0, 0, 0, 0, 0, 0, 0), [])
        self._detection_lock = Lock()
        self._handshakes_received = 0
        self._packets_sent = 0

    # ---------- public API (preserved) ----------

    def setDetectionData(self, data):
        """Update the payload that will be written on the next poll."""
        with self._detection_lock:
            self._detections = data

    def get_stats(self):
        s = super().get_stats()
        s["handshakes_received"] = self._handshakes_received
        s["packets_sent"] = self._packets_sent
        return s

    # ---------- SerialLink overrides ----------

    def _find_port(self):
        if self._explicit_port is not None:
            return self._explicit_port
        return super()._find_port()

    def _handle_session(self, ser):
        while not self._stop_event.is_set():
            line = ser.readline()
            if not line:
                # read_timeout fired; loop back and re-check stop_event
                continue
            self._record_rx(len(line))
            # errors="replace" defends against stray non-ASCII bytes on the
            # line without killing the thread; we just won't match the handshake.
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded == self._HANDSHAKE:
                with self._detection_lock:
                    packet_bytes = V5SerialPacket(
                        self._MAP_PACKET_TYPE, self._detections).to_Serial()
                n = ser.write(packet_bytes)
                self._record_tx(n)
                self._handshakes_received += 1
                self._packets_sent += 1
            elif decoded:
                self._log.debug("unexpected line on data port: %r", decoded)
