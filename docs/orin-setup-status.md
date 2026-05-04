# Orin Setup Status — 2026-05-03

## Current state

- **Hardware:** Jetson AGX Orin Developer Kit, 64 GB, JetPack 6.2 (L4T R36.5.0), Ubuntu 22.04.5, kernel 5.15.185-tegra (OOT variant).
- **Disk:** `/dev/mmcblk0p1` — 57 GB total, ~24 GB used / ~31 GB free. eMMC only; no NVMe in slot.
- **Power:** **`MAXN` (mode 0)** on **Chicony A17-180P4B 20V/9A 180W barrel jack** (Tier-1 OEM, MSI laptop charger). `jetson_clocks` applied. Full performance unlocked. **Do NOT downgrade to USB-C** while running the AI pipeline — confirmed brownout reboot under sustained full-pipeline load on USB-C.
- **Network:** IP `10.0.0.151` on LAN, hostname `robolabs-orin-agx`, SSH alias `orin` from Mac (`~/.ssh/config`).
- **Installed and verified end-to-end:**
  - **librealsense 2.57.7** with `FORCE_RSUSB_BACKEND=ON`, Python bindings + 30 `rs-*` CLI tools, Tegra-specific MIPI/DFU rules.
  - **PyTorch 2.10.0 + torchvision 0.25.0 + cuDSS 0.7.1.6** (Jetson AI Lab CUDA wheels). CUDA 12.6 verified end-to-end with real GPU compute.
  - **pycuda 2026.1** built from source against system CUDA. Sees Orin as device 0, compute capability **(8, 7)** = SM 87 Ampere.
  - **TensorRT 10.3.0** (JetPack-provided, system Python bindings). **Inference path ported from TRT 7/8 → TRT 10** (commit `0fc555c`).
  - **numpy 1.26.4** in user-site (`~/.local/...`), shadowing system 1.21.5. Required for `pycuda.autoinit` (the `pycuda/compyte/dtypes.py` module uses `np.dtype[Any]` generic-style subscripting which needs numpy ≥ 1.25). System numpy untouched.
  - **pyserial 3.5**, **websocket-server 0.6.4**, **Pillow 12.2.0**.
  - **Node 20 LTS** (20.20.2) via NodeSource apt repo, **npm 10.8.2**, `serve` 14.2.6 installed globally.
  - **Firefox 150.0.1** from Mozilla apt repo.
  - **Docker 29.4.2**; `robolabs` is in `docker` and `dialout` groups.
- **Dashboard:** built and serving.
  - **URLs:** `http://localhost:3000` (Orin local) / `http://10.0.0.151:3000` (LAN, including from Mac via Firefox).
  - **`serve` is detached `nohup` + `disown`** — does not survive reboot; restart command in Quick Reference.
- **Camera:** RealSense D435, **exercised** — pipeline opens, depth + color streaming at sustained ~30 fps. Serial `939622072559`, firmware `5.16.0.1`. **Cable replaced** Sunday after a flaky cable caused U1/U2 link-power-state errors and disconnects under load.
- **Backend (`pushback.py`):** **VERIFIED END-TO-END (visual confirmation in Firefox).** Sustained 20+ min run at MAXN with steady ~30 fps, V5 Brain + GPS comms healthy, WebSocket :3030 listening, dashboard rendering live D435 feed + BallBlue/BallRed detections on the field view + V5 packet counter ticking.
- **systemd `vexai` service:** **NOT installed** (deferred — see "Resume here next session" below).

## Sunday session results — what was demonstrated

End-to-end pipeline ran successfully under MAXN power for 20+ minutes continuous, with **visual confirmation in Firefox**:
- TRT 10 engine compiles + caches; subsequent runs load the `.trt` (no rebuild)
- RealSense D435 streaming 640x480 depth + color at sustained ~30 fps
- V5 Brain serial connected (`/dev/ttyACM1`), `data=True` throughout
- GPS Sensor serial connected (`/dev/ttyACM3`), `gps=True` throughout
- WebSocket server on `:3030` accepting connections
- Detections firing (per the `Mean of empty slice` warning at `pushback.py:126`, which CLAUDE.md identifies as the runtime indicator that detections are being produced)
- **Dashboard at `:3000` rendering live data:** D435 camera feed, BallBlue/BallRed detection overlays on the field view, V5 packet counter ticking. Visually verified end-to-end in Firefox on the Orin.

**Two bugs were found and fixed this session, both upstream-PR worthy:**
1. **TRT 7/8 → TRT 10 API port** (commit `0fc555c`). VAIC's `model_backend.py` and `common.py` used positional-binding APIs (`get_binding_shape`, `execute_async_v2`) removed in TRT 10. Hand-ported to the named-tensor API (`get_tensor_shape`, `execute_async_v3` + `set_tensor_address`), keeping pycuda as the CUDA wrapper and preserving public signatures so `model_backend.py`'s call sites are untouched.
2. **Dashboard hardcoded WebSocket IP** (commit `33464c6`). `config.socketIP = "10.42.0.1"` (the robot-onboard hotspot IP) broke every deployment topology except hotspot. Replaced with `window.location.hostname` so the dashboard self-configures for hotspot, LAN, localhost, or any future deployment.

**One transient runtime pitfall worth recording for future sessions:**
- `pycuda.autoinit` requires numpy ≥ 1.25 (the `pycuda/compyte/dtypes.py` module uses `np.dtype[Any]` generic-style subscripting). System numpy 1.21.5 fails at module-load time with `TypeError: 'numpy._DTypeMeta' object is not subscriptable`. Resolved by installing numpy 1.26.4 to user-site (`pip install --user "numpy>=1.25,<2.0"`); system numpy untouched. **Note this is a smoke-test gap:** Phase 6's verification used `import pycuda.driver as cuda; cuda.init()` which doesn't traverse `pycuda.tools` and so missed this. `pushback.py`'s import chain via `model.py → model_backend.py → common.py` does. Future Orin smoke tests should include `import pycuda.autoinit`.

## Commits added in this session

Pushed to `alex-robolabs/VAIC_25_26@main`:

| SHA | Subject |
|---|---|
| `b6d6a02` | `fix(dashboard): pin jimp to 0.22 for react-scripts 5 compatibility` (Friday) |
| `308f874` | `fix(dashboard): port camera.tsx to jimp 0.22 API` (Friday) |
| `b15d461` | `docs: add Orin install plan and setup status handoff` (Friday) |
| `0fc555c` | `fix(jetson): port TensorRT inference path from TRT 7/8 to TRT 10 API` (Sunday, amended with `Tested` line after end-to-end verification) |
| `33464c6` | `fix(dashboard): use window.location.hostname for WebSocket URL` (Sunday, amended with `Tested` line after visual verification) |
| (this commit) | `docs: update orin-setup-status with Sunday session results` |

`docs/orin-install-plan.md` is the canonical install reference for any future Orin setup at Robolabs. `docs/orin-setup-status.md` (this file) carries forward.

## Resume here next session

**Goal:** harden the Orin pipeline for sustained tournament-condition operation, then move to systemd-based service install.

### Step 1 (~30 min, P0): Add RealSense self-heal to `pushback.py`

**Why P0:** the current `pushback.py` raises and exits on `RuntimeError: Device disconnected` from the RealSense pipeline. A USB blip = process crash. The fork already has self-healing serial comms (`V5Comm.py` / `V5Position.py` via `serial_link.py`) that reconnect transparently within ~3-5 s. RealSense doesn't — same architectural pattern just hasn't been applied yet.

The Sunday session demonstrated this: a USB cable bump caused both V5 (recovered cleanly) and RealSense (process crash) disconnects. With a good cable + 180W power we ran 5+ min clean. But for tournament resilience, the RealSense layer needs the same self-heal as the comms layer.

Approach (matches `docs/comms-patch.md` pattern):
- Wrap `pipeline.start()` / `wait_for_frames()` in a retry-with-watchdog wrapper.
- On `RuntimeError`-with-disconnect-pattern: log via `vexai.realsense` namespace, sleep brief backoff, re-instantiate `rs.pipeline()` + reconfigure streams + retry start.
- Watchdog timeout consistent with the comms patch (`5.0s`).
- Validate by yanking the cable mid-run and confirming pipeline recovers without process crash.

### Step 2 (~15 min, P1): D435 firmware update

Camera is on **firmware 5.16.0.1**; librealsense 2.57.7 ships **5.17.0.10** as recommended (the bin landed at `~/librealsense/build/D4XX_FW_Image-5.17.0.10.bin` during the Phase 2 cmake config). Intel's release notes for 5.17 cite improved USB-3 link-power-state stability — directly relevant given the U1/U2 disconnect signature we hit Sunday. Safe to flash:

```bash
rs-fw-update -f ~/librealsense/build/D4XX_FW_Image-5.17.0.10.bin
```

Run with the camera plugged in directly (no hubs) and idle (no pipeline open). Verify post-flash with `rs-enumerate-devices` — should report 5.17.0.10.

### Step 3 (~30 min, P1): Phase 5 systemd setup

Read `JetsonExample/Scripts/service.sh` (already done — Sunday) — it writes `/etc/systemd/system/vexai.service` with `ExecStart=/bin/bash run.sh`, `Restart=always`. Two Orin-specific concerns before letting it run:

1. `run.sh` invokes `serve -s build &` from the dashboard dir. If our detached `serve` is already running (or a separate `vexai-dashboard.service` exists), this duplicates and fails to bind `:3000`. Either (a) accept the harmless second-bind error, or (b) split dashboard `serve` into its own systemd unit and patch `run.sh` to skip the embedded `serve` invocation on this machine.
2. `run.sh` exports `PYTHONPATH=...:/usr/local/lib/python3.6` (Nano-era artifact). Harmless on Python 3.10 but cosmetically wrong. Optional cleanup.

Recommended: install both as systemd units —
- `vexai.service` (per VEX's `service.sh`, runs `pushback.py`)
- `vexai-dashboard.service` (a new unit, runs `serve -s build` from the dashboard dir)

Both `After=network-online.target`. Test reboot recovery before declaring complete.

### Step 4 (~10 min, P2): Power/thermal soak under MAXN

Run `pushback.py` continuously for 30+ minutes under MAXN, log `tegrastats` periodically. Confirm:
- Sustained ~30 fps with no degradation
- Thermals stay below `cpu@70°C` / `gpu@70°C` (Orin AGX dev kit fan should keep us well below)
- No throttling indicators in tegrastats
- Power draw stays comfortably under the 180 W brick ceiling

Sunday's 5-min sample showed thermals ~42°C / 37°C and ~8 W on monitored rails (true total ~15-25 W). Plenty of headroom. The 30-min soak just confirms no slow-burn issue.

### Step 5 (later, P3): Upstream PR planning

The TRT 10 port (`0fc555c`) and the jimp fix (`b6d6a02` + `308f874`) are both general-purpose VAIC fixes that benefit anyone running on JetPack 6.2 / Node 20. Worth proposing back to `VEX-Robotics/VAIC_25_26`.

Wait until:
- RealSense self-heal (Step 1 above) is in — the upstream change is more compelling as a coherent "modernize for JetPack 6 + tournament resilience" patchset than as scattered fixes.
- Tested on a Pi if available (the jimp fix especially needs Pi validation since `PiExample/` may share dashboard build path).

## Known issues and context

- **NOPASSWD sudo is ENABLED** — `/etc/sudoers.d/99-robolabs-install` (`robolabs ALL=(ALL) NOPASSWD:ALL`). Intentionally left active across the Sunday session. Revert with `sudo rm /etc/sudoers.d/99-robolabs-install` when no longer needed.
- **`pushback.py` does NOT self-heal RealSense disconnects** — see "Resume here next session, Step 1." Until ported, a USB blip on the camera crashes the process. The comms patch protects V5 + GPS but not RealSense.
- **D435 firmware 5.16.0.1** is one minor revision behind librealsense's recommended 5.17.0.10 — see "Resume here next session, Step 2."
- **Two ESLint warnings** in `src/components/field/detection-layer.tsx:20` (unused `fieldWidth` / `fieldHeight` params). Pre-existing in the fork; cosmetic.
- **CUDA path in `~/.bashrc` is hardcoded** to `/usr/local/cuda-12.6`, not the `/usr/local/cuda` symlink. A future JetPack CUDA bump (e.g. to 12.7) will need a manual edit.
- **`/etc/ld.so.conf.d/jetson-pytorch-extras.conf`** points at `/home/robolabs/.local/.../nvidia/cu12/lib` (user-scoped). Fine for this single-user box; multi-user would require switching to system-pip install + updating the path.
- **numpy 1.26.4 is in `~/.local`**, shadowing system 1.21.5. The system numpy is intentionally untouched (apt-pinned by JetPack). Do not run `pip install numpy` as root.
- **Dashboard `serve` is detached (`nohup` + `disown`)**, not a systemd unit — will not survive reboot. Step 3 above adds a unit.
- **`pushback.py` is not a systemd service** — Step 3 above. The `JetsonExample/Scripts/run.sh` invokes `serve` for the dashboard alongside `pushback.py`; on Orin this will conflict with our existing detached `serve` if both run.

## Quick reference

```
SSH:                       ssh orin
Dashboard URL (Mac):       http://10.0.0.151:3000
Dashboard URL (Orin):      http://localhost:3000
WebSocket data port:       :3030

Pull fork updates:         ssh orin "cd ~/VAIC_25_26 && git pull"

Check dashboard process:   ssh orin "pgrep -af 'serve -s build'"
Kill dashboard:            ssh orin "pkill -f 'serve -s build'"
Restart dashboard:         ssh orin "cd ~/VAIC_25_26/JetsonWebDashboard/vexai-web-dashboard-react && nohup serve -s build > /tmp/serve.log 2>&1 &"

Check pushback.py:         ssh orin "pgrep -af 'python3 pushback.py'"
Kill pushback.py:          ssh orin "pkill -f 'python3 pushback.py'"
Run pushback.py:           ssh orin "cd ~/VAIC_25_26/JetsonExample && nohup python3 pushback.py > /tmp/pushback.log 2>&1 &"
Watch pushback log:        ssh orin "tail -f /tmp/pushback.log"

Power baseline (tegrastats): ssh orin "sudo timeout 30 tegrastats --interval 2000"

Backend dir:               ~/VAIC_25_26/JetsonExample/
Dashboard build dir:       ~/VAIC_25_26/JetsonWebDashboard/vexai-web-dashboard-react/build/
TRT engine cache:          ~/VAIC_25_26/JetsonExample/models/pushback_lite.trt
Install plan reference:    docs/orin-install-plan.md
```
