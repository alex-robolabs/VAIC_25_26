"""State-machine tests for SerialLink.

These tests drive the base class through its five states using a
FakeSerial that scripts byte responses and exceptions. They verify the
observable contract: state transitions, counter updates, lifecycle
discipline, and that `is_healthy()` only returns True in OPERATING.

Timing is compressed (read_timeout ~50 ms, half_open_timeout_s ~300 ms)
so the full suite runs in seconds. The relative ratios match the
production defaults (read_timeout=1.0, max_timeouts=5,
half_open_timeout_s=10) — what we test is the relationship between
those values and the state transitions, not the absolute numbers.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import List, Optional

import pytest
import serial

from link_stats import LinkState
from serial_link import LinkSilent, SerialLink


# ---------- test doubles ----------

class FakeSerial:
    """Mimics the slice of serial.Serial that SerialLink uses.

    Scripted reads via `queue_read(data)`; scripted exceptions via
    `queue_raise(exc)`. Reads that find an empty queue block up to
    `timeout` seconds (matching pyserial), then return b'' to signal a
    read-timeout.

    Writes are recorded in `self.writes` for assertion.
    """

    def __init__(self, port, baudrate, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._queue = collections.deque()
        self._raise_next: Optional[BaseException] = None
        self._closed = False
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self.writes: List[bytes] = []

    def queue_read(self, data: bytes) -> None:
        with self._cv:
            self._queue.append(data)
            self._cv.notify_all()

    def queue_raise(self, exc: BaseException) -> None:
        with self._cv:
            self._raise_next = exc
            self._cv.notify_all()

    def _next_chunk(self) -> bytes:
        with self._cv:
            if self._closed:
                raise serial.SerialException("port closed")
            if self._raise_next is not None:
                exc, self._raise_next = self._raise_next, None
                raise exc
            if self._queue:
                return self._queue.popleft()
            self._cv.wait(timeout=self.timeout)
            if self._closed:
                raise serial.SerialException("port closed")
            if self._raise_next is not None:
                exc, self._raise_next = self._raise_next, None
                raise exc
            if self._queue:
                return self._queue.popleft()
            return b''

    def read(self, n: int = 1) -> bytes:
        return self._next_chunk()

    def readline(self) -> bytes:
        return self._next_chunk()

    def read_until(self, terminator: bytes) -> bytes:
        return self._next_chunk()

    def write(self, data: bytes) -> int:
        with self._lock:
            if self._closed:
                raise serial.SerialException("port closed")
            self.writes.append(bytes(data))
        return len(data)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass


class FakeSerialFactory:
    """Callable that creates a fresh FakeSerial per invocation. Tests
    inspect `self.instances` to drive the IO thread."""

    def __init__(self):
        self.instances: List[FakeSerial] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def __call__(self, port, baudrate, timeout=1.0) -> FakeSerial:
        ser = FakeSerial(port, baudrate, timeout)
        with self._cv:
            self.instances.append(ser)
            self._cv.notify_all()
        return ser

    def wait_for_instance(self, n: int, timeout: float = 2.0) -> FakeSerial:
        deadline = time.monotonic() + timeout
        with self._cv:
            while len(self.instances) < n:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(
                        f"only {len(self.instances)} fake-serial(s) created, expected {n}")
                self._cv.wait(timeout=remaining)
        return self.instances[n - 1]


class _PassthroughLink(SerialLink):
    """Minimal SerialLink subclass: reads small chunks and records each
    result. No protocol parsing — used only to exercise the base
    class's state machine."""

    def _handle_session(self, ser: serial.Serial) -> None:
        while not self._stop_event.is_set():
            data = ser.read(64)
            self._record_read(data)


# ---------- helpers ----------

def _make_link(factory: FakeSerialFactory,
               read_timeout: float = 0.05,
               max_timeouts: int = 3,
               half_open_timeout_s: float = 0.3,
               backoff_initial: float = 0.02,
               backoff_max: float = 0.05) -> _PassthroughLink:
    return _PassthroughLink(
        name="test",
        port_filter=lambda d: True,
        baudrate=115200,
        read_timeout=read_timeout,
        max_timeouts=max_timeouts,
        backoff_initial=backoff_initial,
        backoff_max=backoff_max,
        explicit_port="/dev/fake",
        _half_open_timeout_s=half_open_timeout_s,
        _serial_factory=factory,
    )


def _wait_for_state(link: SerialLink, expected: LinkState,
                    timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if link.state() == expected:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"link did not reach {expected}; current state is {link.state()}")


# ---------- construction & lifecycle ----------

def test_construction_is_pure():
    """No IO, no thread spawn from __init__. State starts DOWN."""
    factory = FakeSerialFactory()
    link = _make_link(factory)
    assert link.state() == LinkState.DOWN
    assert link._thread is None
    assert factory.instances == []


def test_start_transitions_to_half_open_when_port_opens():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        _wait_for_state(link, LinkState.HALF_OPEN, timeout=1.0)
    finally:
        link.stop()


def test_stop_returns_state_to_down():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    _wait_for_state(link, LinkState.HALF_OPEN)
    link.stop()
    assert link.state() == LinkState.DOWN


def test_context_manager_starts_and_stops():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    with link as inside:
        assert inside is link
        _wait_for_state(link, LinkState.HALF_OPEN)
    assert link.state() == LinkState.DOWN


def test_double_start_is_a_no_op_warning():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        _wait_for_state(link, LinkState.HALF_OPEN)
        link.start()  # should warn and do nothing
        # only one fake-serial created (the one from the first start)
        time.sleep(0.1)
        assert len(factory.instances) == 1
    finally:
        link.stop()


# ---------- state transitions on data ----------

def test_first_bytes_transition_half_open_to_operating():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        ser = factory.wait_for_instance(1)
        _wait_for_state(link, LinkState.HALF_OPEN)
        ser.queue_read(b'hello')
        _wait_for_state(link, LinkState.OPERATING)
    finally:
        link.stop()


def test_silence_in_operating_transitions_to_degraded():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        ser = factory.wait_for_instance(1)
        ser.queue_read(b'X')
        _wait_for_state(link, LinkState.OPERATING)
        # Stop feeding; one read_timeout should drop us into DEGRADED
        _wait_for_state(link, LinkState.DEGRADED, timeout=1.0)
    finally:
        link.stop()


def test_bytes_in_degraded_recover_to_operating():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        ser = factory.wait_for_instance(1)
        ser.queue_read(b'X')
        _wait_for_state(link, LinkState.OPERATING)
        _wait_for_state(link, LinkState.DEGRADED, timeout=1.0)
        ser.queue_read(b'Y')
        _wait_for_state(link, LinkState.OPERATING, timeout=1.0)
    finally:
        link.stop()


def test_sustained_silence_in_degraded_triggers_reconnect():
    factory = FakeSerialFactory()
    link = _make_link(factory, max_timeouts=3, read_timeout=0.05)
    link.start()
    try:
        first_ser = factory.wait_for_instance(1)
        first_ser.queue_read(b'X')
        _wait_for_state(link, LinkState.OPERATING)
        # Don't feed more bytes. After max_timeouts * read_timeout (~150ms),
        # the link should raise LinkSilent and the base class should
        # spin up a fresh serial via the factory.
        second_ser = factory.wait_for_instance(2, timeout=2.0)
        assert second_ser is not first_ser
        # reconnects counter should have advanced
        assert link.stats().reconnects >= 1
    finally:
        link.stop()


# ---------- HALF_OPEN_TIMEOUT_S ----------

def test_half_open_silence_triggers_reconnect():
    factory = FakeSerialFactory()
    # half_open budget short so the test is fast
    link = _make_link(factory, half_open_timeout_s=0.15, read_timeout=0.05)
    link.start()
    try:
        # First fake-serial sees no bytes at all; should be torn down
        # after half_open_timeout_s and a second fake-serial created.
        factory.wait_for_instance(1)
        second_ser = factory.wait_for_instance(2, timeout=2.0)
        assert second_ser is not factory.instances[0]
    finally:
        link.stop()


def test_bytes_during_half_open_prevent_timeout():
    """If bytes arrive during HALF_OPEN, the link transitions to
    OPERATING and the half-open timeout no longer applies."""
    factory = FakeSerialFactory()
    link = _make_link(factory, half_open_timeout_s=0.15, read_timeout=0.05,
                      max_timeouts=10)
    link.start()
    try:
        ser = factory.wait_for_instance(1)
        ser.queue_read(b'X')
        _wait_for_state(link, LinkState.OPERATING, timeout=0.5)
        # Keep feeding occasional bytes to stay OPERATING
        for _ in range(5):
            time.sleep(0.05)
            ser.queue_read(b'Y')
        # Still on the same serial — no reconnect should have happened
        assert len(factory.instances) == 1
        assert link.stats().reconnects == 0
    finally:
        link.stop()


# ---------- exception path ----------

def test_serial_exception_triggers_reconnect():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        first_ser = factory.wait_for_instance(1)
        first_ser.queue_raise(serial.SerialException("simulated unplug"))
        second_ser = factory.wait_for_instance(2, timeout=2.0)
        assert second_ser is not first_ser
        assert link.stats().reconnects >= 1
    finally:
        link.stop()


# ---------- stats and is_healthy ----------

def test_is_healthy_only_true_in_operating():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    assert link.is_healthy() is False  # DOWN
    link.start()
    try:
        _wait_for_state(link, LinkState.HALF_OPEN)
        assert link.is_healthy() is False  # HALF_OPEN
        ser = factory.wait_for_instance(1)
        ser.queue_read(b'data')
        _wait_for_state(link, LinkState.OPERATING)
        assert link.is_healthy() is True
        _wait_for_state(link, LinkState.DEGRADED, timeout=1.0)
        assert link.is_healthy() is False  # DEGRADED
    finally:
        link.stop()
    assert link.is_healthy() is False  # DOWN


def test_bytes_read_counter_increments():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        ser = factory.wait_for_instance(1)
        ser.queue_read(b'hello')
        _wait_for_state(link, LinkState.OPERATING)
        # Allow another loop iteration for any in-flight increment
        time.sleep(0.05)
        assert link.stats().bytes_read >= 5
    finally:
        link.stop()


def test_stats_returns_snapshot_not_live_reference():
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        snap1 = link.stats()
        ser = factory.wait_for_instance(1)
        ser.queue_read(b'abcdef')
        _wait_for_state(link, LinkState.OPERATING)
        time.sleep(0.05)
        snap2 = link.stats()
        # snap1 was taken before any bytes; mutating it shouldn't bleed
        # into the live state, and snap2 should reflect the bytes.
        assert snap1.bytes_read == 0
        assert snap2.bytes_read >= 6
    finally:
        link.stop()


def test_reconnect_counter_does_not_increment_on_clean_stop():
    """stop() closes the port, which causes a SerialException in the
    reader — but stop_event is set, so the run loop should exit before
    bumping reconnects."""
    factory = FakeSerialFactory()
    link = _make_link(factory)
    link.start()
    try:
        ser = factory.wait_for_instance(1)
        ser.queue_read(b'X')
        _wait_for_state(link, LinkState.OPERATING)
    finally:
        link.stop()
    assert link.stats().reconnects == 0


# ---------- LinkSilent semantics ----------

def test_linksilent_is_an_exception_subclass():
    """LinkSilent is the internal signal used to break out of a session
    when silence exceeds the budget. It must derive from Exception so
    the base class's broad except doesn't suppress it accidentally."""
    assert issubclass(LinkSilent, Exception)
