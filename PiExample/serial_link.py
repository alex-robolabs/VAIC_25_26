"""Self-healing serial connection manager for V5 USB links.

The base class owns one IO thread per instance. Subclasses override
`_handle_session(ser)` with their protocol-specific read/write loop, and
call `_record_read(data)` and `_record_write(n)` so the base class can
maintain the link state machine and stats.

State machine (see docs/pi-comms-design.md for the design rationale):

    DOWN -> CONNECTING -> HALF_OPEN -> OPERATING <-> DEGRADED -> DOWN -> ...

Transitions:
  - DOWN:        no port held; backoff between attempts.
  - CONNECTING:  scanning for a matching device, opening the port.
  - HALF_OPEN:   port open, no bytes received yet from the V5. Times out
                 after HALF_OPEN_TIMEOUT_S to recycle wrong-port cases.
  - OPERATING:   port open, bytes flowing in, writes returning success.
                 The best signal we can produce from this side; does NOT
                 prove the V5 received our writes (wire protocol is
                 effectively one-way for detection data — V5 LCD packet
                 counter is ground truth).
  - DEGRADED:    was OPERATING, V5 has gone briefly silent. Recovers to
                 OPERATING on next bytes; falls to DOWN after the
                 configured silence budget.

There is no separate watchdog thread. Silence detection lives inside the
reader, driven by `read_timeout` ticks: every `read_timeout` seconds with
no bytes increments a counter, and counter >= max_timeouts raises
`LinkSilent`, which the base class treats like any other connection
failure (reconnect with backoff).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from typing import Callable, Optional

import serial
from serial.tools.list_ports import comports

from link_stats import LinkState, LinkStats


# Protocol-coupled constant. V5 cold-boot poll startup takes a few
# seconds; 10s gives generous headroom while still recycling permanently
# silent ports (wrong port, V5 off, hub wedged) within a usable window.
# Not exposed via env var — coupled to V5 boot behavior, not site
# conditions.
HALF_OPEN_TIMEOUT_S = 10.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logging.getLogger("vexai").warning(
            "ignoring invalid %s=%r; using default %.3f", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.getLogger("vexai").warning(
            "ignoring invalid %s=%r; using default %d", name, raw, default)
        return default


class LinkSilent(Exception):
    """Raised by `_record_read` when the silence budget for the current
    state is exceeded. The base class catches this and reconnects."""


class SerialLink:
    """Base class: manages one self-healing serial connection.

    Construction is pure — no IO, no thread. Call `start()` to begin
    operation, `stop()` to end it. Or use as a context manager:

        with SubclassOfSerialLink() as link:
            ...   # link is started; stop() runs on exit

    Subclasses override `_handle_session(ser)` and call `_record_read`
    after every read attempt and `_record_write` after every successful
    write.

    Parameters
    ----------
    name : str
        Short identifier used in logs and the logger namespace
        (e.g. "v5-data" -> logger "vexai.v5-data").
    port_filter : callable(ListPortInfo) -> bool
        Predicate used to pick a device from `comports()`.
    baudrate : int
    read_timeout : float
        pyserial read timeout. Drives the granularity of silence
        detection and stop_event polling. Override via env var
        VEXAI_V5_READ_TIMEOUT_S.
    max_timeouts : int
        Consecutive zero-byte reads tolerated in OPERATING/DEGRADED
        before declaring the link silent. Effective dead-link budget =
        max_timeouts * read_timeout. Override via env var
        VEXAI_V5_MAX_TIMEOUTS.
    backoff_initial / backoff_max : float
        Reconnect backoff bounds. Overrides via env vars
        VEXAI_V5_RECONNECT_BACKOFF_MIN_S / _MAX_S.
    explicit_port : str or None
        If provided, skip `comports()` discovery and open this device
        directly. Useful for tests and for hard-pinning in the field.
    """

    def __init__(self,
                 name: str,
                 port_filter: Callable,
                 baudrate: int = 115200,
                 read_timeout: float = 1.0,
                 max_timeouts: int = 5,
                 backoff_initial: float = 0.5,
                 backoff_max: float = 3.0,
                 explicit_port: Optional[str] = None,
                 *,
                 # Internal test seams. Not part of the public API; do
                 # not pass these from production callers. Underscore
                 # prefix + keyword-only position is the signal.
                 _half_open_timeout_s: float = HALF_OPEN_TIMEOUT_S,
                 _serial_factory: Callable = serial.Serial):
        self.name = name
        self.port_filter = port_filter
        self.baudrate = baudrate
        self.read_timeout = _env_float("VEXAI_V5_READ_TIMEOUT_S", read_timeout)
        self.max_timeouts = _env_int("VEXAI_V5_MAX_TIMEOUTS", max_timeouts)
        self.backoff_initial = _env_float(
            "VEXAI_V5_RECONNECT_BACKOFF_MIN_S", backoff_initial)
        self.backoff_max = _env_float(
            "VEXAI_V5_RECONNECT_BACKOFF_MAX_S", backoff_max)
        self.explicit_port = explicit_port
        self.health_log_interval_s = _env_float(
            "VEXAI_HEALTH_LOG_INTERVAL_S", 30.0)
        self._half_open_timeout_s = _half_open_timeout_s
        self._serial_factory = _serial_factory

        self._log = logging.getLogger("vexai." + name)
        self._stats = LinkStats()
        self._stats_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ser: Optional[serial.Serial] = None
        self._ser_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_silences = 0
        self._last_health_log_at = 0.0

    # ---------- public API ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._log.warning("start() called but link is already running")
            return
        self._stop_event.clear()
        self._consecutive_silences = 0
        with self._stats_lock:
            self._stats.started_at = time.monotonic()
            self._stats.state = LinkState.DOWN
        self._thread = threading.Thread(
            target=self._run, name=self.name + "-link", daemon=True)
        self._thread.start()
        self._log.info(
            "started (baud=%d read_timeout=%.2fs max_timeouts=%d)",
            self.baudrate, self.read_timeout, self.max_timeouts)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        with self._ser_lock:
            ser = self._ser
        if ser is not None:
            with contextlib.suppress(Exception):
                ser.close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._stats_lock:
            self._stats.state = LinkState.DOWN
        self._log.info("stopped")

    def __enter__(self) -> "SerialLink":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def state(self) -> LinkState:
        with self._stats_lock:
            return self._stats.state

    def is_healthy(self) -> bool:
        """OPERATING means we're as healthy as we can prove from this
        side: bytes are flowing in from the V5 and our writes are
        returning success. It does NOT prove the V5 has consumed our
        writes — the V5 LCD dashboard packet counter is the ground truth
        for that. This caveat is load-bearing; do not weaken it."""
        return self.state() == LinkState.OPERATING

    def stats(self) -> LinkStats:
        with self._stats_lock:
            return self._stats.snapshot()

    # ---------- subclass hook ----------

    def _handle_session(self, ser: serial.Serial) -> None:
        """Override me.

        Called after the port opens. Should loop reading and writing
        until either `self._stop_event` is set (return cleanly) or an
        exception is raised (will trigger reconnect).

        Contract:
          - Call `self._record_read(data)` after every read attempt,
            where `data` is the bytes returned (possibly empty if the
            read timed out with no data). The base class manages the
            silence counter and state machine; do not catch
            `LinkSilent` — let it propagate.
          - Call `self._record_write(n)` after each successful write.
          - Do not catch `serial.SerialException`; let it propagate so
            the base class can handle reconnection.
        """
        raise NotImplementedError

    # ---------- helpers for subclasses ----------

    def _record_read(self, data: bytes) -> None:
        now = time.monotonic()
        if data:
            self._consecutive_silences = 0
            with self._stats_lock:
                self._stats.bytes_read += len(data)
                self._stats.last_rx_at = now
                if self._stats.state in (LinkState.HALF_OPEN, LinkState.DEGRADED):
                    prior = self._stats.state
                    self._stats.state = LinkState.OPERATING
                    transitioned = prior
                else:
                    transitioned = None
            if transitioned == LinkState.HALF_OPEN:
                self._log.info("first bytes received → OPERATING")
            elif transitioned == LinkState.DEGRADED:
                self._log.info("recovered → OPERATING")
        else:
            self._consecutive_silences += 1
            with self._stats_lock:
                state = self._stats.state
                connected_at = self._stats.connected_at
                last_rx_at = self._stats.last_rx_at
            if state == LinkState.HALF_OPEN:
                silence_s = now - (last_rx_at or connected_at)
                if silence_s >= self._half_open_timeout_s:
                    self._log.warning(
                        "HALF_OPEN > %.1fs without bytes; reconnecting",
                        silence_s)
                    raise LinkSilent("half-open timeout")
            elif state == LinkState.OPERATING:
                with self._stats_lock:
                    self._stats.state = LinkState.DEGRADED
                self._log.info(
                    "no bytes for %.1fs → DEGRADED", self.read_timeout)
            elif state == LinkState.DEGRADED:
                if self._consecutive_silences >= self.max_timeouts:
                    self._log.warning(
                        "DEGRADED for %d consecutive timeouts (~%.1fs); reconnecting",
                        self._consecutive_silences,
                        self._consecutive_silences * self.read_timeout)
                    raise LinkSilent("degraded timeout")
        self._maybe_emit_health_log(now)

    def _record_write(self, n: int) -> None:
        now = time.monotonic()
        with self._stats_lock:
            self._stats.bytes_written += n
            self._stats.last_tx_at = now
            if n > 0:
                self._stats.packets_out += 1

    def _record_packet_in(self) -> None:
        with self._stats_lock:
            self._stats.packets_in += 1

    def _record_parse_error(self, err: BaseException) -> None:
        with self._stats_lock:
            self._stats.parse_errors += 1
            self._stats.last_error = f"{type(err).__name__}: {err}"

    # ---------- internals ----------

    def _find_port(self) -> Optional[str]:
        if self.explicit_port is not None:
            return self.explicit_port
        try:
            for dev in comports():
                if self.port_filter(dev):
                    return dev.device
        except Exception as e:
            self._log.warning("comports() failed: %s", e)
        return None

    def _record_error(self, err: BaseException) -> None:
        with self._stats_lock:
            self._stats.last_error = f"{type(err).__name__}: {err}"

    def _maybe_emit_health_log(self, now: float) -> None:
        if now - self._last_health_log_at < self.health_log_interval_s:
            return
        self._last_health_log_at = now
        snap = self.stats()
        self._log.info(
            "health: state=%s rx=%d tx=%d pkt_in=%d pkt_out=%d "
            "reconnects=%d t_since_rx=%.1fs t_since_bidir=%.1fs",
            snap.state.value,
            snap.bytes_read,
            snap.bytes_written,
            snap.packets_in,
            snap.packets_out,
            snap.reconnects,
            snap.time_since_last_packet_s,
            snap.time_since_last_bidirectional_s,
        )

    def _run(self) -> None:
        backoff = self.backoff_initial
        while not self._stop_event.is_set():
            with self._stats_lock:
                self._stats.state = LinkState.CONNECTING

            port = self._find_port()
            if port is None:
                self._log.debug("no matching device; retry in %.1fs", backoff)
                if self._stop_event.wait(backoff):
                    break
                backoff = min(backoff * 1.5, self.backoff_max)
                continue

            try:
                self._log.info("opening %s", port)
                ser = self._serial_factory(
                    port, self.baudrate, timeout=self.read_timeout)
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                with self._ser_lock:
                    self._ser = ser
                now = time.monotonic()
                with self._stats_lock:
                    self._stats.state = LinkState.HALF_OPEN
                    self._stats.port_name = port
                    self._stats.connected_at = now
                self._consecutive_silences = 0
                self._log.info("opened %s → HALF_OPEN", port)
                backoff = self.backoff_initial

                self._handle_session(ser)

                if self._stop_event.is_set():
                    self._log.info("session ended (stop requested)")
                else:
                    self._log.warning(
                        "session returned without error; reconnecting")
            except LinkSilent as e:
                self._record_error(e)
                self._log.info("link silent: %s", e)
            except serial.SerialException as e:
                self._record_error(e)
                self._log.warning("serial error on %s: %s", port, e)
            except Exception as e:
                self._record_error(e)
                self._log.exception("unexpected error on %s", port)
            finally:
                with self._ser_lock:
                    if self._ser is not None:
                        with contextlib.suppress(Exception):
                            self._ser.close()
                        self._ser = None

            if self._stop_event.is_set():
                break
            with self._stats_lock:
                self._stats.reconnects += 1
                self._stats.state = LinkState.DOWN
            if self._stop_event.wait(backoff):
                break
            backoff = min(backoff * 1.5, self.backoff_max)

        with self._stats_lock:
            self._stats.state = LinkState.DOWN
        self._log.info("link thread exiting")
