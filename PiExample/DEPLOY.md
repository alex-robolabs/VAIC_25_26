# Deploying the Pi-side VEX AI Comms Layer

This is the deployment guide for the Raspberry Pi 5 + Coral USB
Accelerator side of the VEX AI stack. It covers first-time per-Pi
setup (`bootstrap.sh`), ongoing patch deploys (`deploy_pi.sh`), the
verification checklist, common failure modes, and rollback.

If you're picking up a Pi that's already running and you just want to
check it's working, jump to **[Success signal](#-success-signal--the-only-one-that-matters)**.

For the design behind the redesign — why the comms layer is the way it
is — see **[`docs/pi-comms-design.md`](../docs/pi-comms-design.md)**.

---

## What's in PiExample

All under `PiExample/`:

| File | Purpose |
| --- | --- |
| `serial_link.py` | Base class. Five-state machine (DOWN, CONNECTING, HALF_OPEN, OPERATING, DEGRADED), reconnect-forever with backoff, watchdog folded into the reader, structured journald health log. |
| `link_stats.py` | `LinkState` enum + `LinkStats` dataclass. Snapshots returned via `link.stats()`. |
| `V5Comm.py` | `V5SerialComms` over `SerialLink`. Protocol classes (`AIRecord`, `V5SerialPacket`, etc.) preserved byte-for-byte from upstream. |
| `V5Position.py` | `V5GPS` over `SerialLink`. New pure `decode_gps_frame()` function for testable framing logic. |
| `vexai_logging.py` | journald logger setup; honors `VEXAI_LOG_LEVEL`. |
| `show_ports.py` | USB descriptor diagnostic — run this when a deploy doesn't take. |
| `pushback.py` | Main loop. Adapted from upstream with `ExitStack` lifecycle for V5 + GPS links and SIGTERM handler. |
| `filter.py` | Small `LiveFilter` GPS smoothing helper. Copied unchanged from upstream. |
| `__init__.py` | Re-exports for `from PiExample import V5SerialComms, V5GPS, ...`. |
| `tests/unit/test_protocol_parsing.py` | Encoder + GPS-frame decoder tests. |
| `tests/unit/test_link_state_machine.py` | State-machine tests using a `FakeSerial` double. |
| `Scripts/bootstrap.sh` | One-time per-host setup (run before first deploy). |
| `Scripts/deploy_pi.sh` | Ongoing patch deploy from your Mac. |
| `Scripts/run.sh` | systemd ExecStart entrypoint. Sets `PYTHONPATH` so PiExample shadows the upstream stock files at import time. |
| `Scripts/vexai.service` | systemd unit. Restart=always, env-var ops knobs. |
| `Scripts/service.sh` | Idempotent unit installer (called by deploy_pi.sh). |
| `Scripts/restart.sh` | One-command restart helper with optional USB rebind. |

PiExample is self-contained for the **comms layer**. `pushback.py`
also imports `V5Web`, `V5MapPosition`, `model`, `model_backend`,
`data_processing` — those live in `JetsonExample/` (unchanged from
upstream) and are reached at runtime via `PYTHONPATH` set by
`Scripts/run.sh`.

The Nano fleet's patch lives in `JetsonExample/`. **PiExample does not
modify any file in JetsonExample.**

## Dependencies

None new. The Pi stock VEX image already has:

- Python 3.9 via `pyenv` (pycoral does not yet build on 3.10+, so the
  pyenv pin is intentional)
- `pyserial` 3.5
- `numpy`, `cv2`, `pyrealsense2`, `pycoral` (from VEX setup)

No `pip install` required. No `apt-get install` required.

---

## First-time setup: `bootstrap.sh`

**Run this once per new Pi**, before the first deploy. After that,
`deploy_pi.sh` runs without prompting.

### What it does

`bootstrap.sh` codifies the per-host setup that used to be a checklist
copy-pasted from `CLAUDE.md`. It's idempotent — running it twice is a
no-op, never destructive.

Four steps, each skipped if already done:

1. **`ssh-copy-id`** — pushes your Mac's `~/.ssh/id_ed25519.pub` to the
   Pi's `authorized_keys`. Skipped if the key already authenticates.
2. **NOPASSWD sudoers entry** at `/etc/sudoers.d/vexai`. Grants
   passwordless sudo for **only** the four commands the deploy and
   restart scripts need. Each grant is justified inline in the script:
     - `/bin/systemctl` — start/stop/restart the `vexai` service
     - `/usr/bin/journalctl` — tail the journal during deploy verification
     - `/usr/bin/tee` — write `/etc/systemd/system/vexai.service`
       (shell redirects can't be sudo'd directly), and write
       `/sys/.../authorized` during `restart.sh --usb`
     - `/usr/bin/pkill` — sweep stale `pushback.py` and `serve -s build`
       processes during restart
3. **Hostname rename** to the fleet convention (`pi-fifteen`, `pi-twentyfour`,
   etc. — size-based to match the robot they live on). `/etc/hosts` is
   updated independently so `sudo` doesn't print "unable to resolve
   host" warnings.
4. **Regenerate SSH host keys** to break the cloned-image fingerprint
   collision common in fresh Pi imager flashes. A marker at
   `~/.ssh/.vexai-bootstrap-done` makes the step idempotent across
   re-runs even though the ssh session drops mid-step. The new ED25519
   fingerprint is printed for capture into the fleet inventory.

### How to run it

From your Mac, with the Pi reachable on the network:

```bash
cd ~/Projects/VEX\ AI/VAIC_25_26/PiExample/Scripts
./bootstrap.sh <target-ip> <hostname>
```

Examples:

```bash
./bootstrap.sh 10.0.0.20 pi-fifteen        # 15-inch competition robot
./bootstrap.sh 10.0.0.21 pi-twentyfour     # 24-inch competition robot
```

Hostname rules: must match `pi-[a-z]+` (lowercase letters, no digits,
no hyphens beyond the leading `pi-`). The script enforces this up
front.

### What you'll see

```
[bootstrap] target:   vex@10.0.0.20
[bootstrap] hostname: pi-fifteen

[bootstrap] checking reachability...
[bootstrap] step 1/4: ssh key auth
  authorizing /Users/you/.ssh/id_ed25519.pub (one password prompt)
[bootstrap] step 2/4: sudoers entry
  installing /etc/sudoers.d/vexai
[bootstrap] step 3/4: hostname rename
  renaming raspberrypi → pi-fifteen
  checking /etc/hosts for 127.0.1.1 → pi-fifteen
[bootstrap] step 4/4: regenerate SSH host keys
  regenerating /etc/ssh/ssh_host_*

  new ED25519 fingerprint:
    256 SHA256:OD5z...Fz0 root@pi-fifteen (ED25519)

  ⚠️  capture this fingerprint into CLAUDE.md's SSH host key table

[bootstrap] done. pi-fifteen (10.0.0.20) is ready for:
    ./deploy_pi.sh 10.0.0.20
```

The "one password prompt" appears once, the first time you run
`bootstrap.sh` for a Pi. Re-runs skip it (key already authorized).

> ℹ️ The `Pseudo-terminal will not be allocated because stdin is not a
> terminal` lines are harmless — they appear when `ssh -t` is used in a
> non-interactive context. Ignore.

---

## Ongoing deploys: `deploy_pi.sh`

After bootstrap, every code update goes through `deploy_pi.sh`.

### How to run it

```bash
cd ~/Projects/VEX\ AI/VAIC_25_26/PiExample/Scripts
./deploy_pi.sh <pi-ip-or-hostname>
```

Example:

```bash
./deploy_pi.sh 10.0.0.20
```

### What it does

1. **Backup.** Tar the existing comms files on the Pi to
   `~/vexai-pi-backup-YYYYMMDD-HHMMSS.tgz`. Rollback uses this.
2. **Copy.** scp the redesigned Python files, scripts, and tests to
   `~/VAIC_25_26/PiExample/`. Idempotent — overwrites in place.
3. **Install systemd unit.** Copy `vexai.service` to
   `/etc/systemd/system/`, daemon-reload, enable, restart.
4. **Wait + verify.** Sleep 15 s for the import-heavy pushback to come
   up, then grep the journal for state transitions and errors.
5. **Print V5 LCD reminder.** Pi-side OPERATING is necessary but not
   sufficient. The V5 LCD packet counter is the ground truth.

### What you should see in the verification window

A clean deploy produces a journal trace like:

```
opening /dev/ttyACM3
opened /dev/ttyACM3 → HALF_OPEN
first bytes received → OPERATING
health: state=OPERATING rx=8484 tx=93128 pkt_in=706 pkt_out=706 reconnects=15
```

The `reconnects=15` is fine — those reconnect cycles happen during the
~10–60 s before the V5 program starts pumping handshake bytes. Once
bytes flow, reconnects freezes at whatever count was reached.

The `pushback` line later tells you both links: `data=True gps=True
data_state=OPERATING gps_state=OPERATING`.

If you see HALF_OPEN warnings continuing past the verification window,
go to **[Troubleshooting](#-troubleshooting)** below.

---

## ✅ Success signal — the only one that matters

**The V5 Brain LCD shows `Packets` counter non-zero and increasing.**

That's the deploy-succeeded signal. Walk over to the robot, look at
the LCD.

Everything else on the Pi side — `systemctl is-active vexai` reporting
active, `journalctl` showing `data_state=OPERATING`, `is_healthy()`
returning True — is **necessary but not sufficient**. The state
machine is honest about Pi-side health, but it cannot prove the V5 has
*consumed* the packets we wrote. Only the V5's own LCD counter
confirms that.

This is documented loudly in `is_healthy()`'s docstring on purpose. We
chose to make the limitation impossible to miss.

---

## 🔍 Troubleshooting

### ⚠️ FIRST, ALWAYS: verify the V5 side before touching Pi code

Most "the patch isn't working" reports are actually "the V5 isn't in a
state where there's anything for the patch to work against." Before
debugging anything on the Pi:

1. **V5 Brain powered on?**
2. **Is `ai_demo` actually running on the V5?** Downloaded is not
   enough. The V5 USB User Port endpoint that the comms layer reads
   from only enumerates while a user program is *executing*. If the
   program has crashed, been stopped, or is waiting for competition
   field enable, you won't see it.
3. **USB cable seated at both ends?**
4. **Run `show_ports.py` on the Pi** and confirm **all four** expected
   endpoints are present:

   ```bash
   ssh vex@<pi-ip> 'python3 ~/VAIC_25_26/PiExample/show_ports.py'
   ```

   Expected output:

   ```
   /dev/ttyACM0     | GPS Sensor - Vex Robotics Communications Port
   /dev/ttyACM1     | GPS Sensor - Vex Robotics User Port
   /dev/ttyACM2     | VEX Robotics V5 Brain - <serial> - VEX Robotics Communications Port
   /dev/ttyACM3     | VEX Robotics V5 Brain - <serial> - VEX Robotics User Port
   ```

   Device numbers may shuffle. Descriptions must match.

   **If the V5 Brain User Port is missing:** stop debugging Pi code.
   Power on the Brain and start `ai_demo` first. We watched our
   patched stack loop through HALF_OPEN/reconnect for several minutes
   during the first Pi deploy because ai_demo wasn't running — the
   state machine reported the truth, the failure was off-Pi.

### Pi journal shows `data_state=OPERATING` but V5 LCD shows `Packets=0`

This is the failure mode the design specifically warns about. Two
sub-cases:

**Sub-case A: wrong USB port selected (filter ambiguous).** The V5
Brain User Port and the GPS Sensor User Port both contain "User" in
their descriptors. The filter at `V5Comm.py:148` is:

```python
port_filter=lambda d: "V5" in d.description and "User" in d.description
```

Both substrings — V5 AND User — are required. If your V5 firmware ever
ships with a descriptor that drops "V5" or replaces it with something
else, the filter will fall through to whatever the next "User" port
is, which would be GPS, and you'd open the wrong port. `show_ports.py`
is the diagnostic.

**Sub-case B: V5 program is executing but not pumping handshakes.**
Some `ai_demo` configurations only send the AA55CC3301 handshake when
the program is in a specific competition mode. If the V5 is running
but in a state that doesn't poll the Pi, you'll see User Port
enumerated AND zero bytes. Confirm by running `cat /dev/ttyACM3` (with
vexai stopped first) and watching for handshake-line bytes.

### Service active, no `OPERATING` state, only `HALF_OPEN > 10.0s without bytes`

The Pi's reader is opening the port (so `show_ports.py` selected the
right one) but no bytes are arriving. Same root cause as sub-case B
above: V5 not actually pumping. Check the V5 LCD: is `ai_demo`
selected and showing as running? Are there any error messages on the
Brain?

### `bootstrap.sh` fails at step 4 (host key regen)

The marker at `~/.ssh/.vexai-bootstrap-done` is written *before* the
ssh restart so the step is idempotent. If the script reports an
error during the restart-ssh phase, that's expected — the active
session drops as part of restarting sshd. The Mac-side `ssh-keygen
-R` clears the stale fingerprint, and the post-regen reconnect
captures the new key. If it doesn't reconnect, sshd may not have
come back up — check from another machine on the network.

### `deploy_pi.sh` shows "Pseudo-terminal will not be allocated"

Cosmetic. `ssh -t` requests a pty for sudo prompts; in non-interactive
contexts (called from another script, etc.) the request is denied with
that warning, but the remote command runs anyway. Ignore.

### `sudo: unable to resolve host pi-fifteen`

Cosmetic. Means `/etc/hosts` doesn't have an entry mapping `127.0.1.1`
to your Pi's hostname. The current `bootstrap.sh` ensures it does.
Earlier-bootstrapped Pis from before this fix may need a manual
update:

```bash
ssh vex@<pi-ip> 'sudo sed -i.bak "s/^127\.0\.1\.1.*/127.0.1.1\t$(hostname)/" /etc/hosts'
```

Doesn't affect functionality — `vexai`, `ssh`, `systemctl`, V5 comms
all work fine. Just clutters command output.

### Tests are failing on the Pi

```bash
ssh vex@<pi-ip>
cd ~/VAIC_25_26/PiExample
python3 -m pytest tests/unit/ -v
```

42 tests, all should pass. If they don't, it's a real regression and
we want to hear about it before deploy. Don't suppress.

---

## Day-to-day dev loop

### Restart the pipeline without rebooting

```bash
ssh vex@<pi-ip> '~/VAIC_25_26/PiExample/Scripts/restart.sh'
```

Flags:

```bash
./restart.sh --logs          # tail journal after restart
./restart.sh --usb           # also rebind V5 USB devices (use if port wedged)
./restart.sh --usb --logs
```

### Stop the pipeline (to run pushback.py manually)

```bash
ssh vex@<pi-ip>
sudo systemctl stop vexai
cd ~/VAIC_25_26/PiExample
PYTHONPATH=~/VAIC_25_26/PiExample:~/VAIC_25_26/JetsonExample python3 pushback.py
sudo systemctl start vexai   # resume auto-start
```

### Watch the health line

```bash
ssh vex@<pi-ip> 'sudo journalctl -u vexai -f' | grep health
# pushback: health: data=True gps=True data_state=OPERATING gps_state=OPERATING fps=15.6
# v5-data:  health: state=OPERATING rx=19284 tx=208328 pkt_in=1606 ...
# v5-gps:   health: state=OPERATING rx=192656 tx=0 pkt_in=12040 ...
```

Each link emits one structured health line every 30 s
(`VEXAI_HEALTH_LOG_INTERVAL_S`).

### Filter logs by component

```bash
ssh vex@<pi-ip> 'sudo journalctl -u vexai | grep v5-data'    # V5 data link only
ssh vex@<pi-ip> 'sudo journalctl -u vexai | grep v5-gps'     # GPS link only
ssh vex@<pi-ip> 'sudo journalctl -u vexai | grep HALF_OPEN'  # reconnect events
```

### Tune ops knobs without redeploying

Create a systemd drop-in (no edit to the unit file in the repo):

```bash
ssh vex@<pi-ip>
sudo mkdir -p /etc/systemd/system/vexai.service.d
sudo tee /etc/systemd/system/vexai.service.d/local.conf <<'EOF'
[Service]
Environment="VEXAI_LOG_LEVEL=DEBUG"
EOF
sudo systemctl daemon-reload
sudo systemctl restart vexai
```

Available knobs (defaults in `Scripts/vexai.service`):

| Variable | Default | What it controls |
| --- | --- | --- |
| `VEXAI_LOG_LEVEL` | `INFO` | Python logging level |
| `VEXAI_HEALTH_LOG_INTERVAL_S` | `30` | Seconds between structured health log lines |
| `VEXAI_V5_READ_TIMEOUT_S` | `1.0` | pyserial read timeout (granularity of silence detection) |
| `VEXAI_V5_MAX_TIMEOUTS` | `5` | Consecutive timeouts before declaring DEGRADED → DOWN |
| `VEXAI_V5_RECONNECT_BACKOFF_MIN_S` | `0.5` | Initial reconnect backoff |
| `VEXAI_V5_RECONNECT_BACKOFF_MAX_S` | `3.0` | Max reconnect backoff |

Effective dead-link budget = `MAX_TIMEOUTS × READ_TIMEOUT` = 5 s by
default. The HALF_OPEN startup budget is hardcoded at 10 s in
`serial_link.py` — coupled to V5 boot polling behavior, not site
conditions, so not env-var-tunable.

### Check descriptors any time

```bash
ssh vex@<pi-ip> 'python3 ~/VAIC_25_26/PiExample/show_ports.py'
```

---

## Rollback

If a deploy misbehaves:

```bash
ssh vex@<pi-ip> 'cd ~/VAIC_25_26/PiExample && \
    tar xzf ~/vexai-pi-backup-*.tgz && \
    sudo systemctl restart vexai'
```

This restores the previously-deployed comms files (`V5Comm.py`,
`V5Position.py`, `serial_link.py`, `link_stats.py`, `vexai_logging.py`,
`pushback.py`) from the tarball `deploy_pi.sh` writes at the start of
every deploy. ~30 seconds and you're back to the prior state.

If you've never deployed before and need to rip out PiExample
entirely:

```bash
ssh vex@<pi-ip> 'sudo systemctl stop vexai && \
    sudo systemctl disable vexai && \
    sudo rm -f /etc/systemd/system/vexai.service && \
    sudo systemctl daemon-reload && \
    rm -rf ~/VAIC_25_26/PiExample'
```

After this the Pi is back to stock VEX state minus the systemd unit.
JetsonExample/ on the Pi is untouched throughout.

---

## FAQ

**Q: Does the V5 brain code need to change?**
No. The wire protocol is byte-identical to upstream. V5 ai_demo runs
unchanged.

**Q: Does this affect match-day behavior?**
No — the new comms layer is strictly a superset. Match-day startup
still connects within a few seconds, still respects `Restart=always`
on crash. The new behavior only kicks in if the original would have
silently died.

**Q: Why are some unchanged dependencies (V5Web, V5MapPosition, model)
still in JetsonExample/ and not copied to PiExample/?**
Because they're outside the comms-patch scope and unchanged from
upstream. Copying them would just mean N more files to keep in sync
with upstream. `Scripts/run.sh` sets `PYTHONPATH` so PiExample's
redesigned `V5Comm.py` and `V5Position.py` shadow the stock files at
import time, while V5Web and V5MapPosition resolve to the JetsonExample
copies. When the Nano fleet is fully retired, we'll fold these into
PiExample for full isolation; until then, the PYTHONPATH approach
avoids divergence.

**Q: What's the Pi-vs-Nano comms difference at runtime?**
Architecturally the same: one IO thread per link, per-host self-healing
reconnect, structured journald logging. Visible differences:

- Nano patch's binary `is_healthy()` → Pi's five-state machine with an
  `OPERATING`-only boolean wrapper (and a docstring loudly explaining
  the limit of what the boolean can prove)
- Nano patch's separate watchdog thread → Pi's silence detection
  folded into the reader's read-timeout loop (no cross-thread port
  close)
- Nano patch's "thread starts in `__init__`" → Pi's pure construction
  + explicit `start()`/`stop()` + context-manager idiom
- Nano patch's hardcoded knobs → Pi's env-var ops knobs in the
  systemd unit

None of these change the wire protocol or the match-day behavior.

**Q: Are there tests on the Pi?**
Yes. 42 unit tests in `tests/unit/`. Run with `python3 -m pytest
tests/unit/` from the PiExample directory. They cover protocol
encoding (CRC, framing, round-trip), GPS frame decoding (good frame,
truncated, oversized, bad terminator, partial terminator), and the
five-state link state machine (transitions, timer-driven reconnect,
SerialException recovery, snapshot semantics).

**Q: How do I add a Pi to the fleet?**
1. Image the SD card from the VEX Pi installer.
2. First boot, change Wi-Fi if needed, get the Pi on the network.
3. From your Mac: `bootstrap.sh <ip> pi-<size>` (one password prompt).
4. `deploy_pi.sh <ip>`.
5. V5 LCD packet counter > 0 → done.

**Q: Why "pi-fifteen" / "pi-twentyfour" instead of color names like
the Nanos (`vex-red`, `vex-white`)?**
The Pis live on the size-class platforms: 15-inch competition robot
gets `pi-fifteen`, 24-inch gets `pi-twentyfour`. The Nano fleet's
color naming was tied to the physical Jetson case colors — Pi cases
all look the same, so size is the more durable identifier.

**Q: What if the Pi is offline (network down, field deploy)?**
Same recipe as the Nano's USB-stick deploy in
`JetsonExample/DEPLOY.md`, applied to PiExample. Copy the files to a
USB stick, mount on the Pi, copy into `~/VAIC_25_26/PiExample/`,
restart the service. The systemd unit and Scripts handle the rest.
