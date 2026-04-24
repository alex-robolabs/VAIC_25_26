# The V5 ↔ Jetson Comms Patch

This is the technical writeup of the self-healing serial comms patch
applied to `JetsonExample/`. It's the companion to
[`JetsonExample/DEPLOY.md`](../JetsonExample/DEPLOY.md) — that doc
covers *how* to deploy; this one covers *what changed and why*.

Audience: someone picking up this codebase cold, needing to understand
why the comms layer looks the way it does and what tradeoffs are baked
into it. Aim for 5–10 minutes.

## The problem

The VAIC reference architecture splits the autonomy stack across two
computers. A VEX V5 Brain handles motor control, sensor I/O, and
match-state logic. An NVIDIA Jetson Nano (or Raspberry Pi 5) handles
computer vision: it pulls frames from an Intel RealSense depth camera,
runs a TFLite object detection model, projects each detection into
field-frame coordinates, and sends the resulting list of detections
back to the V5 over USB serial. The V5 polls the Jetson at roughly
30 Hz with a short ASCII line (`"AA55CC3301\r\n"`) and expects a
framed binary response carrying the latest detection record. In
steady state this pattern works fine. The problem is everything
around steady state — connection bring-up, disconnects, and silent
stalls.

The reference implementation of `V5SerialComms` fails four distinct
ways in the field, and it does so silently.

The first failure is a hard-coded retry cap. When the Jetson thread
starts, it scans the system's serial ports for one whose USB
description matches the V5 Brain. If the scan comes up empty — which
happens any time the V5 hasn't finished booting, or is being
reprogrammed, or is between USB re-enumerations — the thread waits a
second and retries. After five retries it gives up, executes
`return None`, and the thread exits permanently. There is no outer
loop. There is no restart. The main pushback loop on the Jetson
keeps running, continues to call `setDetectionData()` against the
comms object, and has no way to know that the comms object is now a
corpse. The system looks healthy from the outside and isn't. The
only recovery is to power-cycle the whole rig.

The second failure is the absence of any watchdog on mid-stream
silence. Once the thread has successfully opened a serial port, it
sits in a tight loop reading lines. PySerial's `readline()` blocks
until either a newline arrives or the port's configured timeout
expires (set to 10 seconds in the reference). If the V5 stops
sending — because its user program crashed, because the USB cable
developed a fault, because the V5's USB endpoint stalled without
signalling the kernel — the reader sees a string of empty reads,
loops back, and keeps waiting. Forever. The serial port is open.
The thread is alive. No bytes are flowing. Nothing triggers a
reconnect because nothing notices that reconnection might help.

The third failure is textbook: the reader decodes every line as
UTF-8 (`ser.readline().decode("utf-8").rstrip()`) despite the
protocol being predominantly binary. The V5's handshake happens to
be ASCII, but any byte that arrives outside a valid handshake — noise,
an out-of-spec packet, a garbled re-enumeration transient — raises
`UnicodeDecodeError`. The `except` clause catches `serial.SerialException`
specifically, not broad `Exception`, so the decode error escapes the
function, and the thread dies in the same way as the retry cap. From
the outside, a single bad byte is indistinguishable from a cable pull.

The fourth failure is invisibility. Every diagnostic in the reference
is `print()`. There are no timestamps, no severity levels, no
structured output, no integration with systemd's journal. When any of
the above failures occurs, the fact of failure isn't logged because
the thread is dead, and nothing else knows enough to log on its
behalf. The service keeps running; `systemctl status` says "active";
the operator has no signal short of noticing the robot doesn't react
to AI data anymore. Even then, the journal for the service contains
nothing diagnostic.

## Why it was fragile by design

The reference code wasn't carelessly written — it was written for a
different operational model than the one we're using it in. The code
lives in a VEX-authored repo literally named `JetsonExample`, released
alongside hardware for a specific competition, and it reflects the
assumption that V5 and Jetson power up together (typically with the
Jetson powered from the V5's three-wire port), complete a handshake
within a few seconds, exchange packets for a two-minute match, and
power down together. Inside that operational envelope, a fail-fast
behavior is defensible: if the Jetson can't find the V5 within five
seconds, something is probably genuinely wrong — a bad cable, a
misconfigured program, a wrong USB port — and the student-operator
is better served by visible failure than a silent retry loop. A
mid-match watchdog is irrelevant because the match is two minutes
long. Structured logging is overkill because the target audience is
high-school teams debugging at a kitchen table, not engineers running
post-mortems. The example code solved the example problem. The
failure is operational: nothing *replaced* the example as the
substrate for serious development, and the example got stuck carrying
a load it was never specified to handle. Dozens of reprograms per
hour, battery swaps with the Jetson running, hours-long iteration
sessions — the real dev loop — push past the example's assumptions
about an hour in and never come back.

## What the patch does

The patch extracts the connection management out of `V5SerialComms`
and `V5GPS` into a shared base class, `SerialLink`, and rebuilds it
around the assumption that disconnect events are normal. Four design
decisions shape the new behavior.

**Reconnect forever with bounded backoff.** The discover → open → read
cycle now lives inside an outer loop that only exits when an explicit
stop is requested. If device discovery fails, we wait a short time
(starting at 500 ms, growing exponentially to a 3-second cap) and try
again. If the port opens but the read loop raises, we close what we
have and go back to discovery. There is no attempt counter. A V5
reprogram that takes 15 seconds is just 15 seconds of backoff loops;
the moment the port reappears, the link is back. The cost of a
failed retry is one `comports()` call — negligible.

**Byte-flow watchdog in a separate thread.** The main reader updates a
`last_rx_at` monotonic timestamp every time bytes arrive. A second
thread wakes once a second, checks that timestamp, and if it's older
than the configured watchdog window (5 seconds by default) it closes
the serial port from under the reader. PySerial's next operation on
that port raises `SerialException`, the reader exits, the main loop
catches it, reconnects. This is the whole reason mid-stream stalls
are recoverable: we don't wait for the kernel to tell us the port is
dead, because in the silent-stall failure mode the kernel doesn't
know. We assume prolonged silence means dead and force the
reconnection ourselves.

**Structured logging into journald.** Every significant event —
starting, searching, opening a port, connecting, watchdog trips,
reconnection attempts, errors, shutdown — is logged via Python's
`logging` module to stderr, where systemd captures it into the
journal. Loggers are named per component (`vexai.v5-data`,
`vexai.v5-gps`, `vexai.pushback`), so `journalctl -u vexai | grep
v5-data` shows you the full history of that one link. Timestamps are
included. Severity levels distinguish routine state changes from
errors. What was previously print-noise is now forensic evidence.

**An honest health signal.** The old code had no way to tell the main
loop "I'm connected but not actually receiving anything." The new
`is_healthy()` method returns True only when the link is in the
CONNECTED state *and* the last-received-byte timestamp is within the
watchdog window. The `pushback.py` main loop logs this every 30
seconds as part of a `health:` line that also includes FPS. For the
first time, the journal contains an unambiguous answer to "is data
flowing right now?"

The tradeoff worth naming is the watchdog threshold. Too short and
you thrash on any application-level pause — a V5 program that takes a
little longer than usual to process a frame, a field-control pause
between autonomous and driver control — all look like stalls and
trigger unwanted reconnects. Too long and real stalls hide for the
full window before anyone notices. We picked 5 seconds based on the
observed handshake rate (~30 Hz, so 5s is ~150 missed polls) and the
fact that no benign pause in the system should approach that length.
This is a dial worth revisiting if hardware or firmware timing
characteristics change.

## What the patch does NOT fix

Be honest about the boundaries:

- **Wire protocol is unchanged.** If the V5 firmware stops sending
  handshakes at all — program crash, wrong competition state, field
  disabled — the Jetson will reconnect cleanly but has no one to
  talk to. The watchdog will trip, the link will re-open, and the
  cycle will repeat harmlessly. That's not a bug, but it is a
  failure mode that `is_healthy()` will flag the same way it flags a
  real comms problem. Distinguishing the two requires looking at
  the V5 side.

- **`is_healthy()` verifies flow, not correctness.** The Jetson
  could receive well-formed handshakes and respond with malformed
  `AIRecord` packets; the link would report healthy while the V5
  rejected every response. Adding CRC validation on *inbound*
  packets is in scope for a future revision, but only becomes
  relevant when we extend the protocol (e.g., adding V5 → Jetson
  pose packets).

- **USB descriptor strings are not stable across firmware versions.**
  The filter uses two substrings (`"V5"` AND `"User"`) to uniquely
  identify the V5 Brain User Port. A firmware update that changes
  the USB CDC `iInterface` string could silently break this.
  `show_ports.py` is the diagnostic; hard-coding exact strings would
  add brittleness, not robustness.

- **Kernel-level USB is out of scope.** If the USB subsystem itself
  wedges (a power glitch, a bus hang), no user-space recovery
  helps. `restart.sh --usb` covers the common case by toggling the
  `authorized` sysfs flag for VEX devices, which is functionally
  equivalent to unplugging and replugging the cable.

## What we learned deploying

Two lessons from the first real deployment (Jetson #1, the one in
Rod's office) are captured in `DEPLOY.md`'s troubleshooting section
and worth pulling into this writeup because they're not obvious from
the code alone.

The first lesson: the V5 Brain's USB endpoints are state-dependent.
On that Jetson, an early debugging session ran `comports()` before
the V5 had started its user program and saw only two endpoints
labeled as GPS Sensor ports. That snapshot made it look as though
the V5 Brain didn't have a "User Port" at all, which led to a filter
change from `"V5" + "User"` to `"Communications"`. Later, when the
V5 program started running, all four endpoints appeared — but by
then the broken filter ambiguously matched both the V5 Brain's
Communications Port *and* the GPS Sensor's Communications Port. The
order `comports()` returns them isn't specified, so the Jetson
would sometimes open the right port, sometimes the wrong one, and
the symptom was a long tail of intermittent data=True flashes from
the old watchdog code. The fix was reverting the filter. The
*prevention* is to always start from a running V5 before drawing any
conclusions from `comports()`. We added `show_ports.py` as a
first-class diagnostic for this reason, and DEPLOY.md now leads its
troubleshooting section with the single question "is the V5 on,
running ai_demo, and do you see all four ports?"

The second lesson: when the Jetson is listening on the wrong port,
the Jetson-side health signal is actively misleading for a short
window. Opening an unused port succeeds. On a successful open, we
seed `last_rx_at = time.monotonic()` to prevent a spurious watchdog
trip while the link settles. So for the first watchdog window after
connect, `is_healthy()` returns True — even if no byte ever arrives.
After the window, the watchdog trips, the link reconnects, the same
cycle repeats. The journal shows a pattern of "connected → data=True
briefly → watchdog tripped → reconnect → connected → data=True
briefly → …" which looks deceptively like "the link is intermittent"
when in fact it's "the Jetson is talking to a port nobody is using."
The practical consequence is that service-active and data=True are
both necessary-but-not-sufficient signals. The only ground truth is
the V5 Brain's LCD dashboard showing `Packets` counting up. DEPLOY.md
calls this out as *the* success signal.

These feel obvious in hindsight. They were not obvious at 4pm on a
Thursday when we first deployed.
