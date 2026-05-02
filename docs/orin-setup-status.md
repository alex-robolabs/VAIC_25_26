# Orin Setup Status — 2026-05-01

## Current state

- **Hardware:** Jetson AGX Orin Developer Kit, 64 GB, JetPack 6.2 (L4T R36.5.0), Ubuntu 22.04.5, kernel 5.15.185-tegra (OOT variant).
- **Disk:** `/dev/mmcblk0p1` — 57 GB total, **23 GB used / 32 GB free (43%)**. eMMC only; no NVMe in slot.
- **Power mode:** `MODE_30W` (USB-C powered). Do not switch to MAXN until barrel jack / UPS is in place.
- **Network:** IP `10.0.0.151` on LAN, hostname `robolabs-orin-agx`, SSH alias `orin` from Mac (`~/.ssh/config`).
- **Installed and verified** (Phase 6 import-checks all passed cleanly):
  - **librealsense 2.57.7** with `FORCE_RSUSB_BACKEND=ON`, Python bindings + 30 `rs-*` CLI tools, Tegra-specific MIPI/DFU rules.
  - **PyTorch 2.10.0 + torchvision 0.25.0 + cuDSS 0.7.1.6** (Jetson AI Lab CUDA wheels). CUDA 12.6 verified end-to-end with real GPU compute (`torch.randn(128,128).cuda() @ x.T` → `cuda:0`, finite trace).
  - **pycuda 2026.1** built from source against system CUDA. Sees Orin as device 0, compute capability **(8, 7)** = SM 87 Ampere.
  - **TensorRT 10.3.0** (JetPack-provided, system Python bindings).
  - **pyserial 3.5**, **websocket-server 0.6.4**, **Pillow 12.2.0**.
  - **Node 20 LTS** (20.20.2) via NodeSource apt repo, **npm 10.8.2**, `serve` 14.2.6 installed globally.
  - **Firefox 150.0.1** from Mozilla apt repo.
  - **Docker 29.4.2**; `robolabs` is in `docker` and `dialout` groups.
- **Dashboard:** built and serving.
  - **Process:** pid `56358` — `node /usr/bin/serve -s build` (detached via `nohup` + `disown`)
  - **URLs:** `http://localhost:3000` (Orin local) / `http://10.0.0.151:3000` (LAN, including from this Mac via Firefox)
  - Verified responding **HTTP 200** with React app HTML (`<title>VexAI Dashboard</title>`)
- **Camera:** RealSense physically plugged in but **NOT yet exercised** (no `rs-enumerate-devices`/`realsense-viewer` run yet, no pipeline open).
- **Backend** (`pushback.py` / object detection): **NOT yet run.**
- **systemd `vexai` service:** **NOT installed** (deferred to Phase 5).

## What was installed in this session — commit references

Three commits added to `alex-robolabs/VAIC_25_26@main` during dashboard build:
- `b6d6a02` — pin jimp to 0.22 for react-scripts 5 compatibility
- `308f874` — port camera.tsx to jimp 0.22 API
- (this commit) — docs: add Orin install plan and setup status handoff

See `docs/orin-install-plan.md` for the full Orin-specific install procedure with all deviations from VEX's Nano-era doc.

## Resume here on Sunday

**Goal:** Get end-to-end working — RealSense feed flowing through `pushback.py` detection backend, results visible in the dashboard.

### Step 1 (~5 min): Verify RealSense detection

```bash
ssh orin
lsusb | grep -i intel        # expect Intel RealSense USB device
rs-enumerate-devices         # expect device serial number, firmware, supported streams
# Optional from Orin GUI: realsense-viewer to confirm frames pulling visually
```

### Step 2 (~10 min): Read the detection backend before running

```bash
cat ~/VAIC_25_26/JetsonExample/README.md
```
Identify:
- Entry-point Python script (likely `pushback.py`).
- Expected model files (`.onnx`, `.trt`) and their paths.
- Network ports it opens (likely `:3030` for WebSocket per CLAUDE.md).
- Command-line arguments / environment variables.

Read `JetsonExample/Scripts/run.sh` and `service.sh` — **DO NOT run `service.sh` yet**, just read it. We want to know what it does before letting systemd own it.

### Step 3 (~10–30 min): Run backend manually

- Launch detection pipeline by hand from `~/VAIC_25_26/JetsonExample/`. Don't use systemd yet.
- **First run will compile a TensorRT engine from the `.onnx` model** — expect 5–10 minutes the first time only. Subsequent runs reuse the `.trt` cache.
- Watch for errors:
  - **Hardcoded paths** — VEX may have hardcoded `/home/jetson/...`; our user is `/home/robolabs/...`. Patch as needed.
  - **CUDA OOM** — first run with both PyTorch and TensorRT in the same process can be tight at 30 W.
  - **Port conflicts** — dashboard's `serve` already owns `:3000`; backend likely wants `:3030`.
  - **RealSense init failures** — most common: `pipeline.start()` raises if camera unplugs mid-init.
- **Note: the fork has self-healing comms layers** — if V5 Brain isn't connected, `V5Comm` / `V5Position` will log loudly even when the AI pipeline works fine. That's expected (per `docs/comms-patch.md`).

### Step 4 (~5 min): Verify dashboard lights up

- Refresh Firefox at `http://localhost:3000` (or `http://10.0.0.151:3000` from Mac)
- Expect: live camera feed in the camera widget, detected objects on field overlay, WebSocket indicator green.

## Known issues and context

- **NOPASSWD sudo is ENABLED** — `/etc/sudoers.d/99-robolabs-install` (`robolabs ALL=(ALL) NOPASSWD:ALL`). Intentionally left active for the next session. Revert with `sudo rm /etc/sudoers.d/99-robolabs-install` when no longer needed.
- **Two ESLint warnings** in `src/components/field/detection-layer.tsx` (line 20, unused `fieldWidth` / `fieldHeight` params). Pre-existing in the fork; surfaced by our build because `react-scripts` always emits them. Cosmetic — skip until intentional cleanup pass.
- **CUDA path in `~/.bashrc` is hardcoded** to `/usr/local/cuda-12.6`, not the `/usr/local/cuda` symlink. A future JetPack CUDA bump (e.g. to 12.7) will need a manual edit. Nothing to fix now.
- **`/etc/ld.so.conf.d/jetson-pytorch-extras.conf`** points at `/home/robolabs/.local/.../nvidia/cu12/lib` (user-scoped). Fine for this single-user box. If the Orin is ever multi-user'd, switch to `sudo`-installed pip packages and update the `ld.so.conf.d` path accordingly.
- **Dashboard's `serve` is detached** (`nohup` + `disown`), not a systemd unit. Will **not survive reboot**. Restart command in Quick Reference below.
- **Power mode is MODE_30W on USB-C.** Switch to MAXN (`sudo nvpmodel -m 0 && sudo jetson_clocks`) only once a 19V/4.74A+ barrel jack or UPS is in place. The Orin will brownout / throttle if MAXN is set on USB-C power alone.

## Phase 5 (still deferred, plan after end-to-end works)

- systemd unit for `serve` (dashboard frontend, survive reboot).
- systemd unit for `pushback.py` via VEX's `service.sh` — must read `service.sh` before running as root.
- Upstream PR to `VEX-Robotics/VAIC_25_26` with the jimp fix (only AFTER end-to-end on Orin is validated AND tested on a Pi if available).
- Hostname-renaming-aware refactor of `/etc/ld.so.conf.d/jetson-pytorch-extras.conf` if user is ever renamed.
- Cleanup: prefix-with-underscore the unused params in `detection-layer.tsx` (5-min trivial commit).

## Quick reference

```
SSH:                       ssh orin
Dashboard URL (Mac):       http://10.0.0.151:3000
Dashboard URL (Orin):      http://localhost:3000

Pull fork updates:         ssh orin "cd ~/VAIC_25_26 && git pull"
Check dashboard process:   ssh orin "pgrep -af 'serve -s build'"
Kill dashboard:            ssh orin "pkill -f 'serve -s build'"
Restart dashboard:         ssh orin "cd ~/VAIC_25_26/JetsonWebDashboard/vexai-web-dashboard-react && nohup serve -s build > /tmp/serve.log 2>&1 &"

Backend dir:               ~/VAIC_25_26/JetsonExample/
Install plan reference:    docs/orin-install-plan.md
```
