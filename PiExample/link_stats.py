"""Runtime state and counters for a single self-healing serial link.

`LinkState` is the state-machine vocabulary used across the comms layer.
`LinkStats` holds counters and gauges that the IO thread updates and
external callers read via `link.stats()`. Snapshots are returned by
value so callers cannot mutate the live state.

Naming conventions match the design doc (docs/pi-comms-design.md):

  - Counters are monotonic and grow forever.
  - Gauges are point-in-time. The IO thread refreshes them on every
    read result; callers also call `LinkStats.refresh_gauges(now)` when
    sampling so `time_since_*` fields reflect the snapshot moment, not
    the last update.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field, replace


class LinkState(enum.Enum):
    """Five-state link health vocabulary.

    OPERATING is "as healthy as we can prove from this side": bytes are
    flowing in from the V5 and our writes are returning success. It does
    not prove the V5 has consumed our writes — the V5's LCD packet
    counter is the ground truth for that. See `is_healthy()` docstring.
    """
    DOWN = "DOWN"
    CONNECTING = "CONNECTING"
    HALF_OPEN = "HALF_OPEN"
    OPERATING = "OPERATING"
    DEGRADED = "DEGRADED"


@dataclass
class LinkStats:
    state: LinkState = LinkState.DOWN
    port_name: str = ""
    started_at: float = 0.0
    connected_at: float = 0.0
    last_rx_at: float = 0.0
    last_tx_at: float = 0.0

    bytes_read: int = 0
    bytes_written: int = 0
    packets_in: int = 0
    packets_out: int = 0
    reconnects: int = 0
    parse_errors: int = 0
    write_errors: int = 0

    uptime_s: float = 0.0
    time_since_last_packet_s: float = 0.0
    time_since_last_bidirectional_s: float = 0.0

    last_error: str = ""

    extra: dict = field(default_factory=dict)

    def refresh_gauges(self, now: float) -> None:
        self.uptime_s = (now - self.started_at) if self.started_at else 0.0
        self.time_since_last_packet_s = (
            (now - self.last_rx_at) if self.last_rx_at else 0.0
        )
        if self.last_rx_at and self.last_tx_at:
            self.time_since_last_bidirectional_s = max(
                now - self.last_rx_at, now - self.last_tx_at
            )
        else:
            self.time_since_last_bidirectional_s = 0.0

    def snapshot(self) -> "LinkStats":
        s = replace(self, extra=dict(self.extra))
        s.refresh_gauges(time.monotonic())
        return s

    def to_dict(self) -> dict:
        d = {
            "state": self.state.value,
            "port_name": self.port_name,
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "last_rx_at": self.last_rx_at,
            "last_tx_at": self.last_tx_at,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "packets_in": self.packets_in,
            "packets_out": self.packets_out,
            "reconnects": self.reconnects,
            "parse_errors": self.parse_errors,
            "write_errors": self.write_errors,
            "uptime_s": self.uptime_s,
            "time_since_last_packet_s": self.time_since_last_packet_s,
            "time_since_last_bidirectional_s": self.time_since_last_bidirectional_s,
            "last_error": self.last_error,
        }
        d.update(self.extra)
        return d
