# Deploying the Self-Healing Comms Patch

This patch replaces the fragile V5 ↔ Jetson serial connection manager
with a version that reconnects automatically after V5 reprograms,
battery swaps, USB drops, and silent stalls. The wire protocol is
**unchanged** — no V5 brain firmware changes are required.

For the "why" behind the patch, see [`docs/comms-patch.md`](../docs/comms-patch.md).
This doc is the "how to deploy it" companion.

## What's in the patch

All under `JetsonExample/`:

| File | Status | Purpose |
| --- | --- | --- |
| `serial_link.py` | **New** | Shared base class: reconnect loop + watchdog + structured logging + `is_healthy()` |
| `vexai_logging.py` | **New** | Logging setup (stderr → journald) |
| `show_ports.py` | **New** | USB descriptor diagnostic — run this when things don't work |
| `V5Comm.py` | **Replaced** | Protocol classes preserved verbatim; connection manager rewritten as a `SerialLink` subclass |
| `V5Position.py` | **Replaced** | Same treatment for the GPS link |
| `pushback.py` | **Patched** | Adds logging init and a periodic health line in the main loop |
| `Scripts/restart.sh` | **New** | One-command restart helper with optional USB rebind |
| `Scripts/deploy.sh` | **New** | One-command push to a Jetson (this script) |
| `Scripts/vexai-fan.service` | **New** | Systemd unit: sets PWM fan to 200/255 at boot. Compensates for the custom "VEX" `nvpmodel` profile not defining a fan policy. Installed to `/etc/systemd/system/` and enabled by `deploy.sh`. |

Calibration files (`gps_offsets.json`, `camera_offsets.json`,
`color_correction.json`) are **not touched**. Each Jetson keeps its
own calibration.

## Dependencies

None. The new code uses only the Python standard library plus
`pyserial`, which is already installed on every Jetson in the VAIC
image.

- No `pip install` required.
- No `apt-get install` required.
- No systemd unit changes required (`vexai.service` still works as-is).

---

## Prereqs on each Jetson

1. **Repo present at** `~/VAIC_25_26` (the `JetsonExample/` directory
   inside it is where the script writes). If your repo path differs,
   edit `REMOTE_BASE` at the top of `deploy.sh`.
2. **Remote user owns `~/VAIC_25_26/JetsonExample/`** or has read/write
   access to it. The deploy script writes `.py` files and a tar backup
   there.
3. **Remote user can sudo** (for `systemctl restart vexai` and reading
   the journal). NOPASSWD is strongly recommended — one-time setup below.
4. **SSH key auth is strongly recommended** — one-time setup below.
   Without it, you'll type the SSH password once per deploy (we use
   SSH ControlMaster to keep it to one prompt, but one is still more
   than zero).

### One-time: SSH key auth (recommended)

On your Mac:

```bash
# Generate a key if you don't already have one
ssh-keygen -t ed25519 -f ~/.ssh/jetson -N ""

# Push it to each Jetson (one password prompt per Jetson, forever-after-free)
for h in 192.168.1.10 192.168.1.11 192.168.1.12 192.168.1.13; do
    ssh-copy-id -i ~/.ssh/jetson.pub vex@$h
done

# Add an SSH config block so deploy.sh picks up the key automatically
cat >> ~/.ssh/config <<'EOF'

Host jetson*
  User vex
  IdentityFile ~/.ssh/jetson
EOF
```

Test: `ssh vex@10.0.0.90 true` should succeed with no password prompt.

### One-time: NOPASSWD sudo (recommended)

On each Jetson:

```bash
sudo visudo -f /etc/sudoers.d/vexai
# Add the line:
vex ALL=(ALL) NOPASSWD: /bin/systemctl, /bin/journalctl, /usr/bin/tee, /usr/bin/pkill
```

This grants passwordless sudo only for the specific commands
`deploy.sh` and `restart.sh` need — not a blanket NOPASSWD. Adjust
username if yours isn't `vex`.

> **Path note:** on the JetPack 4.6.1 Ubuntu 18.04 base, `journalctl`
> lives at `/bin/journalctl` (no `/usr/bin/journalctl` symlink). Verify
> on your Jetson with `which journalctl` and adjust the rule if it
> reports a different path. The other three commands (`systemctl`,
> `tee`, `pkill`) match what's on disk by default.

With both of the above set up, `./deploy.sh <host>` runs with zero
prompts.

---

## Deploy options

### Option A — `deploy.sh` (recommended)

One command per Jetson, from your Mac:

```bash
cd ~/Projects/VEX\ AI/VAIC_25_26/JetsonExample/Scripts
./deploy.sh <jetson-ip-or-hostname> [username]
```

Example — deploy to all four:

```bash
for h in 192.168.1.10 192.168.1.11 192.168.1.12 192.168.1.13; do
    ./deploy.sh "$h"
done
```

The script:

1. Backs up the originals on the remote (`~/vexai-backup-YYYYMMDD-HHMMSS.tgz`).
2. Copies the patched Python files into `JetsonExample/`.
3. Copies `restart.sh` into `JetsonExample/Scripts/` and `vexai-fan.service` into `/tmp/`.
4. `chmod +x Scripts/restart.sh`.
5. Installs `vexai-fan.service` to `/etc/systemd/system/` (via `sudo tee`),
   `daemon-reload`, `enable`, `restart` — prints `is-active`, `is-enabled`,
   and the resulting `target_pwm` / `cur_pwm` for verification. Idempotent;
   safe to re-run.
6. `sudo systemctl restart vexai`.
7. Waits 15 s for import-heavy service startup.
8. Greps the journal for `connected on` / `watchdog tripped` / `ERROR`
   lines so you have an immediate signal.
9. Prints a reminder to check the V5 Brain LCD for real packet flow
   (see "Success signal" below).

### Option B — USB stick (no network)

Useful at a field site or when SSH isn't set up yet.

On your Mac, copy the files to a USB stick:

```bash
cd ~/Projects/VEX\ AI/VAIC_25_26/JetsonExample
USB=/Volumes/YOUR_USB_LABEL

mkdir -p "$USB/vexai-patch/Scripts"
cp serial_link.py vexai_logging.py show_ports.py \
   V5Comm.py V5Position.py pushback.py \
   "$USB/vexai-patch/"
cp Scripts/restart.sh Scripts/vexai-fan.service "$USB/vexai-patch/Scripts/"
```

On each Jetson:

```bash
USB=/media/vex/YOUR_USB_LABEL
REPO=~/VAIC_25_26/JetsonExample

cd "$REPO"
tar czf ~/vexai-backup-$(date +%Y%m%d-%H%M%S).tgz V5Comm.py V5Position.py pushback.py
cp "$USB/vexai-patch/"*.py "$REPO/"
cp "$USB/vexai-patch/Scripts/restart.sh" "$REPO/Scripts/"
chmod +x "$REPO/Scripts/restart.sh"

# Install the fan service (idempotent — safe to re-run)
sudo cp "$USB/vexai-patch/Scripts/vexai-fan.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vexai-fan

sudo systemctl restart vexai
```

### Option C — manual `scp`

For one-off deploys without running the script:

```bash
cd ~/Projects/VEX\ AI/VAIC_25_26/JetsonExample
TARGET=vex@10.0.0.90

ssh $TARGET "cd ~/VAIC_25_26/JetsonExample && \
    tar czf ~/vexai-backup-$(date +%Y%m%d-%H%M%S).tgz V5Comm.py V5Position.py pushback.py"

scp serial_link.py vexai_logging.py show_ports.py \
    V5Comm.py V5Position.py pushback.py \
    $TARGET:~/VAIC_25_26/JetsonExample/
scp Scripts/restart.sh $TARGET:~/VAIC_25_26/JetsonExample/Scripts/
scp Scripts/vexai-fan.service $TARGET:/tmp/

ssh -t $TARGET "chmod +x ~/VAIC_25_26/JetsonExample/Scripts/restart.sh && \
                cat /tmp/vexai-fan.service | sudo tee /etc/systemd/system/vexai-fan.service >/dev/null && \
                sudo systemctl daemon-reload && \
                sudo systemctl enable --now vexai-fan && \
                sudo systemctl restart vexai"
```

---

## ✅ Success signal — the only one that matters

**The V5 Brain LCD shows `Packets` counter non-zero and increasing.**

That's it. That's the deploy-succeeded signal.

Everything else on the Jetson side — `systemctl status` reporting
active, `journalctl` showing `data=True`, the `connected on /dev/ttyACM3`
log line — is necessary but **not sufficient**. The Jetson's comms
thread can be happily connected to a port that never receives bytes
(if the filter matches the wrong endpoint), and it will log as
healthy for the first watchdog window. We learned this the hard way
during the first-robot deploy: two hours chasing a "working
according to the Jetson logs, zero packets according to the V5"
problem.

So: before you declare a Jetson deployed, walk over to the robot and
look at the Brain's LCD.

---

## 🔍 Troubleshooting

### ⚠️ FIRST, ALWAYS: verify the V5 side before touching Jetson code

90% of "the patch doesn't work" reports are actually "the V5 isn't in
a state where there's anything for the patch to work against." Before
debugging anything on the Jetson:

1. **V5 Brain powered on?** (Obvious, but.)
2. **Is `ai_demo` actually running on the V5?** Downloaded is not
   enough. The V5's USB endpoints that the patch listens to only
   enumerate while a user program is *executing*. If the program has
   crashed, been stopped, or is waiting for competition field enable,
   you won't see them.
3. **USB cable seated at both ends?**
4. **Run `show_ports.py` on the Jetson** and confirm **all four**
   expected endpoints are present:

   ```bash
   ssh $TARGET 'python3 ~/VAIC_25_26/JetsonExample/show_ports.py'
   ```

   Expected (device numbers may vary; descriptions should match):

   ```
   /dev/ttyACM0     | GPS Sensor - Vex Robotics Communications Port
   /dev/ttyACM1     | GPS Sensor - Vex Robotics User Port
   /dev/ttyACM2     | VEX Robotics V5 Brain - <serial> - VEX Robotics Communications Port
   /dev/ttyACM3     | VEX Robotics V5 Brain - <serial> - VEX Robotics User Port
   ```

   **If the V5 Brain User Port is missing, stop debugging Jetson code.**
   Go power on the Brain and start the program first. This alone would
   have saved us hours on the first deployment.

### Jetson logs show `data=True` but V5 dashboard shows `Packets=0`

You're connected to the wrong port. Run `show_ports.py`, find the line
with `VEX Robotics V5 Brain - ... User Port`, note its device path, and
check `V5Comm.py` line 157:

```python
port_filter=lambda d: "V5" in d.description and "User" in d.description,
```

Both substrings must be present in the filter. If your firmware's
descriptor uses different strings than `V5 Brain` or `User Port`,
adjust the filter to match — but keep both conditions (AND), not one,
or the filter will ambiguously match GPS endpoints too.

### Watchdog trips every 5 s, link reconnects constantly, no real traffic

If `show_ports.py` shows the V5 Brain User Port present AND the filter
is correct AND packets still aren't flowing: the V5 is enumerated but
not sending handshakes. Check that `ai_demo`'s main loop is calling
`jetson_comms.request_map()`. If the V5 program is in a competition
mode waiting for field enable, it may genuinely not be sending.

### `deploy.sh` fails with "no tty present and no askpass program specified"

You have the old version of `deploy.sh`. The current version uses
`ssh -t` to allocate a pty for sudo prompts. Pull the latest from the
repo.

### 5+ password prompts during deploy

The current `deploy.sh` uses SSH ControlMaster to share one
connection across all operations — at most one password prompt per
deploy. If you're seeing more, either (a) you have the old version,
or (b) ControlMaster isn't caching (check `/tmp/vexai-deploy.*.sock`
exists during the deploy). Set up ssh-key auth per the Prereqs
section to eliminate prompts entirely.

### USB descriptor strings differ from what's documented

USB descriptors can vary by VEX firmware version. The filter in
`V5Comm.py` uses two substrings (`"V5"` AND `"User"`) to uniquely
identify the Brain's User Port. If your firmware uses different
strings, `show_ports.py` is the authoritative source — adjust the
filter to match *your* descriptors, but keep the AND-of-two-substrings
pattern to avoid ambiguous matches.

### V5 program isn't auto-starting on Brain power-up

Separate issue, not a Jetson-patch issue — but symptoms look similar.
Check the VEXcode "competition" flag and whether the slot is set as
the autorun slot on the Brain.

---

## Day-to-day dev loop

### Restart the pipeline without rebooting

```bash
~/VAIC_25_26/JetsonExample/Scripts/restart.sh
```

Flags:

```bash
./restart.sh --logs          # tail journal after restart
./restart.sh --usb           # also rebind V5 USB (use if port is wedged)
./restart.sh --usb --logs
```

### Stop the pipeline (to run pushback.py manually)

```bash
sudo systemctl stop vexai
cd ~/VAIC_25_26/JetsonExample
python3 pushback.py          # interactive, Ctrl-C to quit
sudo systemctl start vexai   # resume auto-start
```

### Watch the health line

```bash
sudo journalctl -u vexai -f | grep health
# health: data=True gps=True fps=14.2   (every 30 s)
```

### Filter logs by component

```bash
sudo journalctl -u vexai | grep v5-data    # just the data link
sudo journalctl -u vexai | grep v5-gps     # just the GPS link
sudo journalctl -u vexai | grep watchdog   # just reconnection events
```

### Check descriptors any time

```bash
python3 ~/VAIC_25_26/JetsonExample/show_ports.py
```

---

## Rollback

If a deploy misbehaves:

```bash
ssh $TARGET "cd ~/VAIC_25_26/JetsonExample && \
    tar xzf ~/vexai-backup-*.tgz && \
    sudo systemctl restart vexai"
```

This restores the three originals (`V5Comm.py`, `V5Position.py`,
`pushback.py`) from the tarball `deploy.sh` created before copying.
The newly-added files (`serial_link.py`, `vexai_logging.py`,
`show_ports.py`, `Scripts/restart.sh`) become unused — no cleanup
needed.

---

## FAQ

**Q: Does the V5 brain code need to change?**
No. The wire protocol is byte-identical to the reference. The V5 keeps
polling and streaming exactly as before.

**Q: What if `/dev/ttyACM0` changes numbering after reconnect?**
Handled. We find the port by USB description (`"V5" + "User"` /
`"GPS" + "User"`), not by path. Any `/dev/ttyACM*` assignment works.

**Q: Does this affect match-day behavior?**
No — it's strictly a superset. Match-day startup still connects within
a few seconds, still respects `Restart=always` on crash. The new
behavior only kicks in if the original would have silently died.

**Q: Why do I need `show_ports.py` when `comports()` is a one-liner?**
Because the one-liner's quoting breaks when embedded in shell
pipelines via ssh. Shipping a file removes the escaping risk entirely.
Also useful standalone during any future debugging.

**Q: Are there any new Python dependencies?**
No. Standard library plus `pyserial`, which is already installed.

**Q: What happens if two threads try to use the same port?**
The two links (`v5-data` and `v5-gps`) filter on different USB
descriptions, so they naturally claim different `/dev/ttyACM*`
endpoints. If they ever collide, whichever opened first keeps the
port; the other retries until the port is free. No crash.

**Q: Can I still run `pushback.py` directly for interactive testing?**
Yes. Stop the service first (`sudo systemctl stop vexai`), then run
`python3 pushback.py` in the repo directory. Logs go to stdout/stderr.
