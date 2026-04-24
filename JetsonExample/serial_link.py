"""Self-healing serial connection manager for V5 USB links.

Design goals:
  1. Never give up. Reconnect forever on any failure, with bounded
     exponential backoff.
  2. Detect silent wedges. A watchdog thread closes the port if no data
     arrives for `watchdog_seconds`, forcing the reader's next op to fail
     and triggering reconnection.
  3. Be observable. Every state transition is logged. Live stats are
     exposed via get_stats() for dashboard integration.

Subclasses override `_handle_session(ser)` with their protocol-specific
read/write loop. Everything else — discovery, connect, retry, watchdog,
stats, clean shutdown — is shared.

Typical lifecycle:
    INIT -> SEARCHING -> CONNECTED -> RECONNECTING -> CONNECTED -> ...
                                    \\ (stop() called)
                                     -> STOPPED
"""

import logging
import threading
import time
from typing import Callable, Optional

import serial
from serial.tools.list_ports import comports


class LinkStats:
    """Plain container for link runtime stats. Avoids @dataclass for
    broad Python version compatibility (3.6+)."""

    def __init__(self):
        self.state = "INIT"
        self.port_name = ""
        self.connected_at = 0.0
        self.last_rx_at = 0.0
        self.last_tx_at = 0.0
        self.bytes_rx = 0
        self.bytes_tx = 0
        self.reconnect_count = 0
        self.error_count = 0
        self.last_error = ""

    def to_dict(self):
        return dict(self.__dict__)


class SerialLink:
    """Base class: manages one self-healing serial connection.

    Parameters
    ----------
    name: str
        Short identifier used in logs (e.g. "v5-data", "v5-gps").
    port_filter: callable(ListPortInfo) -> bool
        Called against each entry from pyserial's comports() to pick
        the right device. Lambda-friendly.
    baudrate: int
    read_timeout: float
        PySerial read timeout. Keep small (~1s) so _stop_event is
        checked frequently even while blocked in a read.
    watchdog_seconds: float
        If no bytes arrive for this long while CONNECTED, the watchdog
        closes the port to force a reconnect cycle.
    backoff_initial / backoff_max: float
        Exponential backoff bounds between reconnect attempts.
    """

    def __init__(self,
                 name,                        # type: str
                 port_filter,                 # type: Callable
                 baudrate=115200,             # type: int
                 read_timeout=1.0,            # type: float
                 watchdog_seconds=5.0,        # type: float
                 backoff_initial=0.5,         # type: float
                 backoff_max=3.0):            # type: float
        self.name = name
        self.port_filter = port_filter
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self.watchdog_seconds = watchdog_seconds
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        self._stats = LinkStats()
        self._stats_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ser = None                                        # type: Optional[serial.Serial]
        self._ser_lock = threading.Lock()
        self._thread = None                                     # type: Optional[threading.Thread]
        self._watchdog_thread = None                            # type: Optional[threading.Thread]
        self._log = logging.getLogger("vexai." + name)

    # ---------- public API ----------

    def start(self):
        """Start the link thread and watchdog thread. Non-blocking."""
        if self._thread is not None and self._thread.is_alive():
            self._log.warning("start() called but link is already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=self.name + "-link", daemon=True)
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name=self.name + "-wd", daemon=True)
        self._thread.start()
        self._watchdog_thread.start()
        self._log.info("started (baud=%d watchdog=%.1fs)",
                       self.baudrate, self.watchdog_seconds)

    def stop(self, timeout=2.0):
        """Signal stop, close the port, and join threads with a timeout."""
        self._stop_event.set()
        with self._ser_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=timeout)
        self._log.info("stopped")

    def is_healthy(self):
        """True iff currently connected AND a byte has arrived within
        the watchdog window. Safe to call from any thread."""
        with self._stats_lock:
            if self._stats.state != "CONNECTED":
                return False
            if self._stats.last_rx_at == 0:
                return False
            return (time.monotonic() - self._stats.last_rx_at) < self.watchdog_seconds

    def get_stats(self):
        """Return a dict snapshot of current stats. Safe to call from
        any thread. Subclasses may override to add protocol-specific
        counters by calling super().get_stats() and adding keys."""
        with self._stats_lock:
            return self._stats.to_dict()

    # ---------- subclass hook ----------

    def _handle_session(self, ser):
        """Override me.

        Called once per connection attempt, *after* the port opens
        successfully. Should loop reading/writing until either:
          - self._stop_event is set (return cleanly), or
          - an exception is raised (will be caught and trigger reconnect).

        Do NOT catch SerialException here; let it bubble so the base
        class can handle reconnection. Call self._record_rx(n) and
        self._record_tx(n) after each successful read/write to keep
        the watchdog informed and the stats accurate.
        """
        raise NotImplementedError

    # ---------- helpers for subclasses ----------

    def _record_rx(self, n_bytes):
        now = time.monotonic()
        with self._stats_lock:
            self._stats.last_rx_at = now
            self._stats.bytes_rx += n_bytes

    def _record_tx(self, n_bytes):
        now = time.monotonic()
        with self._stats_lock:
            self._stats.last_tx_at = now
            self._stats.bytes_tx += n_bytes

    # ---------- internals ----------

    def _set_state(self, state, **fields):
        with self._stats_lock:
            self._stats.state = state
            for k, v in fields.items():
                setattr(self._stats, k, v)

    def _record_error(self, err):
        with self._stats_lock:
            self._stats.error_count += 1
            self._stats.last_error = "{}: {}".format(type(err).__name__, err)

    def _find_port(self):
        """Scan comports() and return the first matching device path,
        or None if no match. Subclasses may override to honor an
        explicit port override."""
        try:
            for dev in comports():
                if self.port_filter(dev):
                    return dev.device
        except Exception as e:
            self._log.warning("comports() failed: %s", e)
        return None

    def _run(self):
        backoff = self.backoff_initial
        while not self._stop_event.is_set():
            self._set_state("SEARCHING")
            port = self._find_port()
            if port is None:
                self._log.debug("no matching device; retry in %.1fs", backoff)
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 1.5, self.backoff_max)
                continue

            try:
                self._log.info("opening %s", port)
                ser = serial.Serial(port, self.baudrate, timeout=self.read_timeout)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                with self._ser_lock:
                    self._ser = ser
                now = time.monotonic()
                # last_rx_at seeded to now so the watchdog doesn't trip
                # immediately on a slow initial poll.
                self._set_state("CONNECTED",
                                port_name=port,
                                connected_at=now,
                                last_rx_at=now,
                                last_tx_at=now)
                self._log.info("connected on %s", port)
                backoff = self.backoff_initial

                self._handle_session(ser)

                if self._stop_event.is_set():
                    self._log.info("session ended (stop requested)")
                else:
                    self._log.warning(
                        "session returned without error; reconnecting")
            except serial.SerialException as e:
                self._record_error(e)
                self._log.warning("serial error on %s: %s", port, e)
            except Exception as e:
                self._record_error(e)
                self._log.exception("unexpected error on %s", port)
            finally:
                with self._ser_lock:
                    if self._ser is not None:
                        try:
                            self._ser.close()
                        except Exception:
                            pass
                        self._ser = None

            if self._stop_event.is_set():
                break
            with self._stats_lock:
                self._stats.reconnect_count += 1
            self._set_state("RECONNECTING")
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 1.5, self.backoff_max)

        self._set_state("STOPPED")
        self._log.info("link thread exiting")

    def _watchdog(self):
        """Independent liveness monitor. Wakes every second; if we're
        CONNECTED but haven't seen a byte in `watchdog_seconds`, closes
        the port. The reader's next op will fail, the main loop will
        reconnect."""
        while not self._stop_event.is_set():
            if self._stop_event.wait(1.0):
                break
            with self._stats_lock:
                state = self._stats.state
                last = self._stats.last_rx_at
            if state != "CONNECTED" or last == 0:
                continue
            silence = time.monotonic() - last
            if silence > self.watchdog_seconds:
                self._log.warning(
                    "watchdog tripped: %.1fs of silence; forcing reconnect",
                    silence)
                with self._ser_lock:
                    if self._ser is not None:
                        try:
                            self._ser.close()
                        except Exception:
                            pass
        self._log.debug("watchdog thread exiting")
