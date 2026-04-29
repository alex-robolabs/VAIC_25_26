# Pi Comms Layer — Design Document (v2)

## Strategic framing

Pi is a fork, not a port. The three fielded Nanos keep their existing self-healing patch through the current season; their files in `JetsonExample/` stay untouched. The Pi work lives in this same repo in a sibling directory, `PiExample/`, with its own scripts, services, and tests. Going forward, Pi is the production target; the Nanos are legacy hardware running out the season.

The phase 1 success criterion is narrow: plug the Pi in, it boots, the comms layer comes up automatically, it connects to the V5 Brain, and if the V5 disconnects (USB unplug, V5 reboot, hub flake) the Pi reconnects on its own without human intervention. Match or exceed Nano reliability with cleaner internals. That is the entire phase 1 goal. Anything not directly serving it is phase 2.

The non-negotiables transfer from the Nano work. The V5 wire protocol stays unchanged — V5 firmware is owned elsewhere and is out of scope. The public method names that callers (notably the autonomous nav code, wherever it lives) depend on are committed below as a stated constraint, not an open question. The system has to survive USB unplug, V5 reboot, and USB-hub flake without manual intervention.

A truthful retrospective on the Nano patch is the right starting point. It did the right thing for its moment — replaced VEX's give-up-after-five-attempts retry logic with reconnect-forever exponential backoff, added a watchdog, surfaced structured logging through journald, preserved the wire protocol verbatim. It also had real warts that the Pi design engineers out: the watchdog thread closed the serial port out from under the reader (cosmetic but corrosive log noise); `is_healthy()` returned `True` when the Jetson's port was open even though the V5 saw zero packets (operationally a lie, bit us during deployment); the 5-second watchdog threshold was chosen by feel; construction had side effects (`__init__` started a thread); operational metrics existed only as journald lines you had to grep for; and there were zero automated tests.

## API compatibility (stated constraint)

The new comms classes commit to the public method names existing callers depend on. No caller — including the autonomous nav code — should need changes beyond the import path.

Preserved exactly:

- `V5SerialComms.setDetectionData(...)`
- `V5SerialComms.isConnected() -> bool`
- `V5GPS.getPosition() -> (...)`
- `V5GPS.updateOffset(...)`

New additive accessors:

- `is_healthy() -> bool` — backward-compatible name; thin wrapper over `state() == LinkState.OPERATING`
- `state() -> LinkState` — canonical health interface
- `stats() -> LinkStats` — snapshot of all counters and gauges

If a caller currently relies on a Nano-specific behavior outside this list, surface it during code review and decide explicitly.

## Repository layout

```
PiExample/
├── __init__.py              # re-exports V5SerialComms, V5GPS for clean import
├── V5Link.py                # V5SerialComms class (renamed file, preserved class name)
├── GPSLink.py               # V5GPS class (renamed file, preserved class name)
├── serial_link.py           # base class — threading, lifecycle, stats
├── link_stats.py            # LinkStats dataclass + LinkState enum
├── vexai_logging.py         # journald logger setup
├── show_ports.py            # USB descriptor diagnostic
├── pushback.py              # main loop, adapted: imports from PiExample, no CUDA paths
├── model_backend.py         # already runtime-switches Coral vs CUDA — kept as-is
├── models/                  # pushback_lite.tflite + .onnx
├── tests/
│   ├── unit/
│   │   ├── test_protocol_parsing.py
│   │   └── test_link_state_machine.py
│   └── fixtures/
│       └── *.bin            # canned packet captures
├── Scripts/
│   ├── bootstrap.sh         # per-host first-time setup (parallel work track)
│   ├── deploy_pi.sh         # incremental code deploy
│   ├── run.sh               # systemd ExecStart target
│   ├── service.sh           # service management helper
│   └── vexai.service        # systemd unit (Pi-flavored)
└── DEPLOY.md                # Pi-specific deploy + bootstrap docs
```

Class names stay `V5SerialComms` and `V5GPS` for caller compatibility. File names rename to `V5Link.py` / `GPSLink.py` to mark the redesign and avoid confusion with the Nano files. The `__init__.py` re-exports so callers can write `from PiExample import V5SerialComms, V5GPS` without caring about file layout. `JetsonExample/` is not modified.

## Architecture: threading, one IO thread per link

The Pi runs two long-lived comms loops — one for the V5 Brain, one for the GPS sensor — at roughly 15 Hz each. Asyncio's strength is multiplexing thousands of concurrent operations cheaply; we have two, both effectively forever. The blocking surface (`serial.read()`) is exactly what threading was designed for. pyserial doesn't have first-party async support. And debug ergonomics matter: when a competition robot hangs and someone is staring at journalctl, threading stack traces tell a more obvious story than asyncio task graphs.

Each comms class owns one IO thread — no separate watchdog thread (see below). State shared between the IO thread and external callers (last-packet timestamp, link state, counters) lives in the `LinkStats` dataclass guarded by a single lock. Public methods read the dataclass under the lock and return a snapshot — never expose mutable references. This is the boring-concurrent-Python playbook, and it pays off the first time a new contributor reads the code.

## Health: five-state machine

The link maintains a `LinkState` enum:

- `DOWN` — port not open, not currently connecting
- `CONNECTING` — attempting to open, in backoff
- `HALF_OPEN` — port open but no bytes received from V5 yet (V5 might be off, on the wrong port, or wedged)
- `OPERATING` — bytes flowing in, our writes returning success
- `DEGRADED` — was OPERATING; V5 has been silent for one or more read intervals but not yet long enough to give up

`is_healthy()` returns `state() == OPERATING`. The canonical interface is `state()`. Logs and (in phase 2) the dashboard display the state name.

The docstring on `is_healthy()` says explicitly: **OPERATING means we're as healthy as we can prove from this side; the V5's LCD dashboard packet counter is the ground truth for "the V5 is actually receiving."** This is the single most important piece of documentation in the file. The Nano shipped without it and we paid for the omission.

### HALF_OPEN_TIMEOUT_S — internal, 10 seconds

If the link sits in HALF_OPEN for more than 10 seconds without receiving any bytes, it transitions to DOWN and reconnect backoff begins. Reasoning: a V5 program starting from cold boot takes a few seconds to begin polling the user port; 10s is generous headroom for that startup, and a permanently silent port (wrong port selected, V5 powered off, hub wedged) gets recycled and retried within 10s rather than sitting in a state that visually looks like "open."

This is a protocol-tuning constant, not an ops knob — it's coupled to V5 boot behavior, not site conditions. Hardcoded as a module-level constant in `serial_link.py` with a comment explaining the value. Not exposed via env var.

## Watchdog: collapsed into the read loop

No separate watchdog thread. The reader does small reads with a per-read timeout (default 1.0 seconds, configurable via `VEXAI_V5_READ_TIMEOUT_S`). On consecutive zero-byte reads, the link transitions OPERATING → DEGRADED → DOWN. The DEGRADED → DOWN threshold is `VEXAI_V5_MAX_TIMEOUTS` (default 5), giving a 5-second total dead-link budget by default — same order of magnitude as the Nano's hardcoded watchdog, expressed as two semantically meaningful knobs.

Eliminates the watchdog thread, eliminates the cross-thread port-close race, and produces a useful intermediate state instead of the Nano's binary "fine-then-broken."

## Object lifecycle

Construction is pure: `V5SerialComms(config)` validates and stores config. No IO, no thread. `start()` opens the port and starts the reader; `stop()` sets the stop flag, joins the thread with a bounded timeout, closes the port. Context-manager support:

```python
with V5SerialComms(config) as v5, V5GPS(config) as gps:
    main_loop(v5, gps)
```

`pushback.py` wraps both links in a single `contextlib.ExitStack` so SIGTERM (sent by `systemctl stop vexai`) propagates: signal handler raises a sentinel, the stack unwinds, both links shut down deterministically before the process exits.

## Observability (phase 1 only)

The comms layer maintains a `LinkStats` dataclass:

- Counters: `reconnects`, `bytes_read`, `bytes_written`, `packets_in`, `packets_out`, `parse_errors`, `write_errors`
- Gauges: `state` (LinkState enum), `uptime_s`, `time_since_last_packet_s`, `time_since_last_bidirectional_s`

Three things happen with this data in phase 1:

1. **Structured journald log every `VEXAI_HEALTH_LOG_INTERVAL_S` seconds** (default 30). All counters and gauges as structured fields. `journalctl --output=json | jq` is the historical query tool. No new dependencies.
2. **`link.state()` and `link.stats()` are public accessors** on each link object, returning a snapshot. Anything that wants live state — including the dashboard, eventually — calls these.
3. **No HTTP endpoint, no dashboard integration, no Prometheus.** Phase 2 work. Documented as a TODO in `DEPLOY.md`.

The discipline here: the data is *available* via clean accessors. Nothing in phase 1 surfaces it interactively. When phase 2 wires it into the dashboard or an HTTP route, the comms layer doesn't change — the integration code calls existing accessors.

## Testing

Two test surfaces:

**Unit tests for protocol parsing.** Pure-function tests over canned `.bin` byte fixtures. Catches V5 firmware drift before it surfaces at runtime. ~100 lines, no extra dependencies, high ROI.

**State-machine tests via fake serial.** A `FakeSerial` class returns scripted byte sequences and raises `SerialException` at scripted moments. Asserts the link transitions through the expected states (DOWN → CONNECTING → HALF_OPEN → OPERATING → DEGRADED → DOWN → ...) and that timer-driven transitions (HALF_OPEN_TIMEOUT_S, MAX_TIMEOUTS) fire correctly. ~200 lines.

Layout: `PiExample/tests/unit/`. Run with `pytest PiExample/tests/`. No CI yet — manual run before deploy is enough for a four-host fleet. Add a GitHub Action when we feel the pain.

Deferred to phase 2: pty-based fake-V5 integration tests.

## Configuration

Hardcoded module-level constants — V5-firmware-coupled, not site-specific:

- baud rate, packet sizes
- port-filter substrings: `"V5"` AND `"User"` (V5Link), `"GPS"` AND `"User"` (GPSLink)
- `HALF_OPEN_TIMEOUT_S = 10`

Env vars in the systemd unit, ops-tunable:

- `VEXAI_LOG_LEVEL` (default `INFO`)
- `VEXAI_HEALTH_LOG_INTERVAL_S` (default `30`)
- `VEXAI_V5_READ_TIMEOUT_S` (default `1.0`)
- `VEXAI_V5_MAX_TIMEOUTS` (default `5`)
- `VEXAI_V5_RECONNECT_BACKOFF_MIN_S` (default `0.5`)
- `VEXAI_V5_RECONNECT_BACKOFF_MAX_S` (default `3.0`)

Per-host ad-hoc overrides via systemd drop-ins (`/etc/systemd/system/vexai.service.d/local.conf`). No new config file.

## Deployment

Two scripts, intentionally simple, parallel work tracks. The comms layer must be deployable to a Pi set up by hand or by `bootstrap.sh`; bootstrap must work on a fresh Pi independent of whether the comms layer is being deployed. Neither blocks the other.

### deploy_pi.sh

Adapted from the Nano's `deploy.sh`. Same shape — scp-based incremental deploy of patched files, idempotent, no destructive ops. Differences from the Nano version:

- Drops the `vexai-fan.service` install block (Pi 5 kernel governor handles cooling).
- Sudoers path fix: `journalctl` lives at `/usr/bin/journalctl` on Bookworm, not `/bin/journalctl` on the Nano's Ubuntu 18.04.
- Strips dead CUDA exports from the deployed `run.sh`.
- Targets `PiExample/` instead of `JetsonExample/`.

No `--check` flag, no `verify.sh`. Phase 2.

### bootstrap.sh

Codifies the four per-host setup steps from CLAUDE.md so the second Pi (imminent) and third Pi (within months) don't require copy-paste from a runbook.

Scope:

- `ssh-copy-id` (push Mac key)
- sudoers entry permitting NOPASSWD on `/bin/systemctl`, `/usr/bin/journalctl`, `/usr/bin/tee`, `/usr/bin/pkill`
- hostname rename to fleet convention (`pi-red`, `pi-white`, etc. — continuing the color scheme from the Nano fleet)
- regenerate SSH host keys (break the cloned-image fingerprint collision)

Constraints:

- **Idempotent.** Running twice is a no-op, never destructive. Each step checks current state before mutating.
- Takes target IP and desired hostname as positional arguments: `./bootstrap.sh 10.0.0.21 pi-red`.
- Validates inputs (hostname matches `pi-<color>`, IP is reachable on `:22`) before doing anything.
- Lives at `PiExample/Scripts/bootstrap.sh`.
- Documented in `PiExample/DEPLOY.md` alongside the `deploy_pi.sh` section.

Implementation order: comms layer first (production-critical path), bootstrap.sh second (before the next Pi gets set up). Both can ship in the same PR or separate ones — whichever is cleaner.

## Open questions

Resolved and removed: cross-platform codebase (Pi-only, decided), API compatibility (stated constraint above), Prometheus path (no, decided), Pi naming (color scheme continues — `pi-red`, `pi-white`, etc.).

Remaining:

1. **V5-side echo for true bidirectional health.** Wire-protocol enhancement that would let us detect "V5 isn't receiving our writes." Requires V5 firmware changes; out of scope for phase 1. Worth a future conversation with whoever owns V5 firmware. Does not block phase 1.
2. **V5 LCD bench test on Pi.** Confirm the V5's packet-counter behavior is identical when talking to a Pi as it is with a Nano. A short bench test against the Pi at 10.0.0.20 with the V5 LCD visible. Part of phase 1 verification, not a design question.

## Phase 2 backlog (deferred — listed so we don't re-debate)

- Dashboard JSON endpoint (`/api/v5-status`) reading from `link.stats()`.
- React dashboard panel showing live link state and counters.
- `deploy_pi.sh --check` for dry-diff reporting.
- `verify.sh` post-deploy health check (auto-runs at end of `deploy_pi.sh`).
- pty-based fake-V5 integration tests.
- Prometheus exporter (only if Prometheus/Grafana stand up for other reasons).
- V5-side firmware echo / heartbeat for true bidirectional health (depends on V5 firmware ownership).

## Summary

| Concern | Phase 1 |
|---|---|
| Concurrency | Threading, one IO thread per link |
| Health | Five-state enum; OPERATING means "as healthy as we can prove from this side" |
| HALF_OPEN | 10s internal timeout, then DOWN and reconnect |
| Watchdog | Folded into reader, two env-var knobs |
| Lifecycle | Pure construction, `start`/`stop`, context manager |
| Observability | LinkStats dataclass, journald log every 30s, public accessors only — no HTTP |
| Tests | pytest for protocol parsing + state machine via FakeSerial |
| Config | Hardcoded protocol constants, env vars for ops knobs |
| Layout | New `PiExample/` directory, sibling to untouched `JetsonExample/` |
| Deployment | `deploy_pi.sh` (adapted from Nano), `bootstrap.sh` as parallel track |
| API | `setDetectionData`, `getPosition`, `isConnected` preserved; `is_healthy`/`state`/`stats` added |

Phase 1 ships when a Pi can be plugged in, boot, connect to a V5 Brain, and survive disconnect/reconnect cycles without intervention — matching Nano reliability with cleaner internals. Everything else is phase 2.
