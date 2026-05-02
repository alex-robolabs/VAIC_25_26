# VEX AI Install Plan — Jetson AGX Orin (JetPack 6.2)

Adapted from VEX's `BuildImageFromScratch.md` (written for Jetson Nano).
Target: Jetson AGX Orin Developer Kit, 64 GB, eMMC-only, JetPack 6.2 (L4T R36.5.0),
Ubuntu 22.04.5, CUDA 12.6, cuDNN 9.3, TensorRT 10.3, Python 3.10.12.

User: `robolabs` on host `ubuntu` (to be renamed `robolabs-orin-agx`).
Reachable from the Mac at `10.0.0.151` (alias `orin`).

> **Conventions in this doc**
> - **[VEX]** = step is unchanged from the VEX doc.
> - **[MOD]** = step adapted for Orin / this season.
> - **[NEW]** = step added (not in VEX doc).
> - **[SKIP]** = VEX step intentionally omitted, with reason.
>
> Don't run anything yet — review first. All `sudo` commands will prompt for the
> `robolabs` password interactively.

---

## Phase 0 — Pre-flight (already done during recon)

- SSH key auth from Mac → Orin (`ssh orin`) verified.
- Survey doc at `~/orin-survey-20260501.md` on the Orin.
- Decisions locked: stay on eMMC, stay at MODE_30W (USB-C power), use NVIDIA
  Jetson wheel index for PyTorch, system-wide Python, add to docker group,
  rename host to `robolabs-orin-agx`.

---

## Phase 1 — System baseline

### 1. Update package lists and upgrade [VEX]
```bash
sudo apt-get update && sudo apt-get -y upgrade
```
Survey showed only the header line in `apt list --upgradable` — should be a no-op
or near-no-op, but worth running before we add packages.

### 2. Set hostname [NEW]
Default is still `ubuntu`. Rename now so prompts/logs/mDNS reflect the device.
```bash
sudo hostnamectl set-hostname robolabs-orin-agx
sudo sed -i 's/127\.0\.1\.1\s\+ubuntu/127.0.1.1\trobolabs-orin-agx/' /etc/hosts
```
Open a new SSH session afterward to pick up the new prompt
(`robolabs@robolabs-orin-agx`). The `orin` SSH alias still works — it points at
the IP, not the hostname.

### 3. Install Python toolchain [MOD]
The survey showed `pip3` is **not** installed. Install it along with the rest of
the standard Python build dependencies VEX requests in their step 2.
```bash
sudo apt-get install -y --no-install-recommends \
  python3 python3-setuptools python3-pip python3-dev
```
After this, verify:
```bash
python3 --version    # expect 3.10.12
pip3 --version       # expect pip 22.x for python 3.10
```

### 4. Install git [VEX]
```bash
sudo apt-get install -y git
```

### 5. Add `robolabs` to the `docker` group [NEW]
Survey confirmed Docker 29.4.2 is installed but `robolabs` isn't in the
`docker` group, so every container call would need `sudo`.
```bash
sudo usermod -aG docker robolabs
```
Group membership doesn't take effect until a new login — log out of the SSH
session and reconnect (`ssh orin`) before running `docker` commands.

---

## Phase 2 — librealsense (build from source)

### 6. Clone librealsense [VEX]
```bash
cd ~
git clone https://github.com/IntelRealSense/librealsense.git
cd librealsense
```

### 7. Install librealsense build dependencies [VEX, +v4l-utils]
**Make sure no RealSense camera is plugged in during the build.**
```bash
sudo apt-get install -y \
  libssl-dev libusb-1.0-0-dev pkg-config libgtk-3-dev \
  libglfw3-dev libgl1-mesa-dev libglu1-mesa-dev at \
  v4l-utils
```
(Combines VEX steps 5 and 6. `git` and `libssl-dev` were already covered.)

> **`v4l-utils` note:** JetPack 6.2 doesn't ship `v4l-utils` preinstalled.
> `setup_udev_rules.sh` (Step 8) does an early `exit 1` if `v4l2-ctl` (provided
> by `v4l-utils`) isn't on `$PATH` — confirmed during first install. Adding it
> here avoids the deviation we hit on first run.

### 8. Install udev rules [VEX]
```bash
./scripts/setup_udev_rules.sh
```

### 9. ~~Patch the kernel for RealSense~~ [SKIP]
**VEX step 8 (`./scripts/patch-realsense-ubuntu-lts.sh`) is skipped.**

Reason: that script patches stock Ubuntu LTS UVC drivers. We're on JetPack's
**custom Tegra kernel 5.15.185-tegra (OOT variant)** — the patches don't apply
cleanly and aren't needed because we'll build librealsense with
`-DFORCE_RSUSB_BACKEND=ON`, which uses libuvc in userspace and bypasses the
kernel UVC driver entirely. (This is the standard approach on Jetson.)

### 10. Configure shell environment for CUDA [VEX, modified context]
The CUDA libraries are installed (survey confirmed `/usr/local/cuda → cuda-12.6`)
but `nvcc` isn't on `$PATH` for new accounts.

> **Pre-check before appending:** Some Orin units arrive with CUDA exports
> already in `~/.bashrc` (e.g. from a partial prior setup or factory image).
> Run this first:
> ```bash
> grep -n 'cuda' ~/.bashrc
> ```
> If exports are already present, **skip the append below** and go straight to
> verification. Watch for hardcoded paths like `/usr/local/cuda-12.6` vs. the
> `/usr/local/cuda` symlink — both work, but the symlink form is forward-
> compatible across CUDA version bumps (a future JetPack update changing the
> default CUDA version would auto-follow the symlink; hardcoded paths break).
> Worth flagging but not fixing during install — note for a later cleanup pass.

Append to `~/.bashrc` only if the pre-check showed no existing CUDA entries:
```bash
cat >> ~/.bashrc <<'EOF'

# CUDA 12.6 (JetPack 6.2)
export PATH=/usr/local/cuda/bin${PATH:+:${PATH}}
export LD_LIBRARY_PATH=/usr/local/cuda/lib64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}
EOF
source ~/.bashrc
```
Verify (works whether the exports were pre-existing or freshly added — but
note: Ubuntu's default `~/.bashrc` has an early-return guard for non-interactive
shells, so non-interactive `ssh host "nvcc ..."` won't see the PATH. Use an
interactive shell or inline the PATH in the test):
```bash
nvcc --version    # expect "Cuda compilation tools, release 12.6"
```

### 11. ~~Bogus PYTHONPATH export~~ [SKIP]
**VEX step 11 (`export PYTHONPATH=$PYTHONPATH:/usr/local/OFF`) is skipped.**

Reason: that line is a doc-bug — a CMake `-DBUILD_…=OFF` option leaked into the
text and got mistaken for a path. There's no `/usr/local/OFF` directory on any
system. Setting this would be harmless but pointless; we omit it.

### 12. Configure the librealsense build [VEX]
```bash
cd ~/librealsense
mkdir -p build && cd build
cmake ../ \
  -DFORCE_RSUSB_BACKEND=ON \
  -DBUILD_PYTHON_BINDINGS:bool=true \
  -DPYTHON_EXECUTABLE=/usr/bin/python3 \
  -DCMAKE_BUILD_TYPE=release \
  -DBUILD_EXAMPLES=true \
  -DBUILD_GRAPHICAL_EXAMPLES=true \
  -DBUILD_WITH_CUDA:bool=true
```

### 13. Compile and install librealsense [MOD]
**Two changes from the VEX literal commands:**

1. **`-j$(nproc)` instead of `-j2`.** Orin AGX has 12 Cortex-A78AE cores and a
   64 GB power envelope; it won't crash mid-build the way a Nano does. Even at
   MODE_30W, thermal headroom is fine. Wall time on first build was 11 min 23 s
   (vs. VEX's stated 30+).
2. **Compile in user-space, only `sudo` for install.** Two reasons:
   - Compilation happens in the user-owned `build/` directory; root isn't
     needed and adds risk (build artifacts owned by root cause permission
     headaches on rebuild).
   - `sudo` strips PATH via `secure_path` in `/etc/sudoers`, which removes
     `/usr/local/cuda-12.6/bin`. The build invokes `nvcc` directly — strip
     CUDA from PATH and the build silently produces a non-CUDA binary or fails
     to link. Running `make` as the regular user keeps PATH intact.

**First build (fresh tree — recommended path):**
```bash
make -j$(nproc)
sudo make install
```

**Rebuild (after changes, with a populated `install_manifest.txt`):**
```bash
sudo make uninstall && sudo make clean
make -j$(nproc)
sudo make install
```

> **Why first builds skip the `uninstall && clean` prefix:** `cmake`'s
> `uninstall` target reads `install_manifest.txt`, which only exists after a
> previous `make install`. On a fresh tree the file is missing, `make
> uninstall` exits non-zero, and the `&&` chain short-circuits before reaching
> `make clean` or the actual build. Confirmed during first install.
Verify the Python binding installs:
```bash
python3 -c 'import pyrealsense2 as rs; print(rs.__version__)'
```

---

## Phase 3 — PyTorch from NVIDIA's Jetson wheel index [NEW]

### 14. Install PyTorch + torchvision + cuDSS (Jetson AI Lab wheels)
Upstream PyPI's `torch` wheels for `aarch64` aren't built with CUDA. We pull
from the Jetson AI Lab index, which serves wheels built against the matching
JetPack / CUDA / Python combo.

**Index URL verified live** (HTTP 200, real ~233 MB CUDA wheel for torch 2.10):
`https://pypi.jetson-ai-lab.io/jp6/cu126/+simple`. Available `cp310-linux_aarch64`
wheel pairs at verification time: torch 2.8.0/2.9.1/2.10.0/2.11.0 with matching
torchvision 0.23.0/0.24.1/0.25.0/0.26.0.

**Pinned versions:**
- `torch==2.10.0` + `torchvision==0.25.0` — one notch back from the latest.
  Reasoning: tournament-bound runtime machine; a torch release with weeks of
  additional community vetting on Jetson is worth more than being on bleeding
  edge.
- `nvidia-cudss-cu12==0.7.1.6` — latest. cuDSS is leaf-load (torch invokes it
  for sparse linalg, which the VEX object detection pipeline doesn't exercise).
  The "one notch back" tournament-safety logic doesn't apply because surface
  area to bugs is near-zero. Latest gives bug fixes without API-churn risk.

#### Step 14a — Upgrade pip
```bash
python3 -m pip install --upgrade pip
```
Brings pip to 26.x in user-site (`~/.local/bin/pip`); subsequent
`python3 -m pip` invocations resolve to it.

#### Step 14b — Install torch + torchvision (CUDA wheels)
```bash
python3 -m pip install \
  --index-url https://pypi.jetson-ai-lab.io/jp6/cu126/+simple \
  --no-deps \
  torch==2.10.0 torchvision==0.25.0
```

> **Why `--no-deps` and only `--index-url` (no `--extra-index-url`):**
>
> An earlier draft of this plan used `--index-url Jetson + --extra-index-url
> PyPI` and let pip resolve deps. **That installs the wrong wheel.** Pip's
> wheel selector ranks wheels by **platform tag specificity** before it
> considers index priority. Both indexes carry torch 2.10.0:
>
> - **Jetson AI Lab:** `torch-2.10.0-cp310-cp310-linux_aarch64.whl` (~233 MB,
>   real CUDA build)
> - **PyPI:** `torch-2.10.0-cp310-cp310-manylinux_2_28_aarch64.whl` (~146 MB,
>   CPU-only)
>
> `manylinux_2_28_aarch64` declares glibc compatibility — pip considers it
> **more specific** than the bare `linux_aarch64` tag and prefers it
> regardless of which index served it. Index priority is only a tiebreaker
> for *otherwise-equal* candidates; tag specificity outranks it.
>
> The fix: `--no-deps` + only `--index-url Jetson` (no PyPI fallback). With
> `--no-deps`, pip won't try to resolve numpy/pillow/etc. from any index — it
> just installs the two pinned wheels. Transitive deps (sympy, jinja2, fsspec,
> filelock, networkx, typing-extensions, mpmath) need to already be present;
> they get pulled in automatically the first time you run `pip install` *with*
> resolution and remain installed even if you later uninstall+reinstall torch
> with `--no-deps`. On a clean Orin, run `python3 -m pip install sympy jinja2
> fsspec filelock networkx typing-extensions mpmath` once before this step.

#### Step 14c — Install cuDSS runtime (co-requisite for torch 2.10+)
```bash
python3 -m pip install \
  --index-url https://pypi.jetson-ai-lab.io/jp6/cu126/+simple \
  --no-deps \
  nvidia-cudss-cu12==0.7.1.6
```

> **Why cuDSS is required:** torch 2.10's `libtorch_cuda.so` has an unresolved
> `NEEDED` entry for `libcudss.so.0` (NVIDIA CUDA Direct Sparse Solver).
> JetPack 6.2 doesn't ship cuDSS — it's not in any apt source on the Orin
> (`apt-cache search cudss` returns empty). Without this wheel, `import torch`
> fails with `ImportError: libcudss.so.0: cannot open shared object file`.
>
> Same `--no-deps` + `--index-url` only pattern as Step 14b, same reasoning.
> The wheel declares `cuda-toolkit` as a Python dep; on Jetson that's
> JetPack-provided system-wide, so `--no-deps` correctly skips it.

#### Step 14d — Make cuDSS visible to the dynamic linker
The cuDSS wheel installs to `~/.local/.../nvidia/cu12/lib/`. torch's
`libtorch_cuda.so` has `RUNPATH=$ORIGIN` (it only searches its own directory),
so the cuDSS libs in the separate wheel directory are unreachable by default.

```bash
echo "/home/robolabs/.local/lib/python3.10/site-packages/nvidia/cu12/lib" | \
  sudo tee /etc/ld.so.conf.d/jetson-pytorch-extras.conf
sudo ldconfig
```

> **Architectural reason:** Jetson AI Lab unbundles cuDSS (and other CUDA
> runtime libs) into separate `nvidia-*-cu12` wheels rather than embedding
> them in `libtorch_cuda.so` (which is what upstream PyPI's CUDA wheels do
> with `+cu12X` builds for x86_64). Smaller torch wheel, more upgrade
> flexibility — but torch's `RUNPATH=$ORIGIN` can't reach the separate wheel's
> `lib/` dir. Adding the path to `/etc/ld.so.conf.d/` resolves it.
>
> **Structurally identical** to how JetPack itself exposes
> `/usr/local/cuda-12.6/lib64` via `/etc/ld.so.conf.d/cuda.conf` — the same
> linker-config pattern, just for a pip-installed CUDA library instead of an
> apt-installed one.
>
> **Future maintenance / multi-user note:** the path above is
> `/home/robolabs/.local/...` because we used a user-site pip install
> (decision: system-wide Python, but installs default to user-site when system
> site-packages isn't writable for a non-root user). If this Orin is ever
> multi-user'd, switch the install to system pip
> (`sudo python3 -m pip install ... --target=/usr/local/lib/python3.10/dist-packages`
> or equivalent) and update the `ld.so.conf.d` entry to point at the new path.

#### Verification (no env-var workarounds)
Open a fresh shell — confirm `LD_LIBRARY_PATH` is empty — then run:
```bash
ldconfig -p | grep cudss        # expect 4 cuDSS .so.0 entries
python3 -m pip check             # expect no torch-related issues
python3 -c 'import torch; \
  print(torch.__version__, "cuda:", torch.cuda.is_available(), \
        torch.version.cuda, "device:", torch.cuda.get_device_name(0))'
# expect: 2.10.0 cuda: True 12.6 device: Orin

python3 -c 'import torch; x = torch.randn(3,3).cuda(); \
  y = x @ x.T; print("device:", y.device, "trace:", y.trace().item())'
# expect: device: cuda:0 trace: <some finite real number>
```
If `torch.cuda.is_available()` is `False`, **stop and debug** — the rest of
the stack is useless without it.

> **numpy note:** the survey showed `numpy 1.21.5` (system package). torch 2.10
> works against it for our purposes (tested), but you may see warnings about
> numpy version. The system numpy can't be safely upgraded via apt on JetPack
> (it would conflict with other system tools); if a newer numpy is needed,
> install via `python3 -m pip install -U "numpy<2.0"` to user-site.

---

## Phase 4 — VEX AI source and Python deps

### 15. Clone the VAIC 25/26 fork [MOD]
**Changed from `VEX-Robotics/VAIC_23_24` to your fork for the current season.**
```bash
cd ~
git clone https://github.com/alex-robolabs/VAIC_25_26.git
cd VAIC_25_26/JetsonExample
```

### 16. Install Python packages [MOD]
**Removed `pip3 install tensorrt`** — TensorRT 10.3.0 is already installed
system-wide via JetPack with working Python bindings (`python3 -c 'import
tensorrt'` returns `10.3.0`). The PyPI `tensorrt` wheel doesn't ship aarch64
Jetson builds, so attempting it fails or — worse — pulls a CPU-only stub that
shadows the real one.

**`pycuda` builds from source** against system CUDA. We have to expose CUDA
headers and libs to the build, plus install Boost (a build-time dep on Jetson):
```bash
sudo apt-get install -y build-essential libboost-python-dev libboost-thread-dev
export PATH=/usr/local/cuda/bin:$PATH
export CPATH=/usr/local/cuda/include:$CPATH
export LIBRARY_PATH=/usr/local/cuda/lib64:$LIBRARY_PATH
pip3 install pycuda
```
Verify:
```bash
python3 -c 'import pycuda.driver as cuda; cuda.init(); print("pycuda devices:", cuda.Device.count())'
```

Then the rest of the VEX-prescribed packages:
```bash
pip3 install pyserial websocket-server
python3 -m pip install --upgrade Pillow
```

### 17. Add `robolabs` to the `dialout` group [MOD]
**VEX's syntax is wrong** (`sudo usermod -a <USERNAME> -G dialout` — the `-a`
takes no argument and `<USERNAME>` is in the wrong position). Correct form:
```bash
sudo usermod -aG dialout robolabs
```
Required for serial access to the V5 Brain. Like the docker group, takes effect
on next login.

---

## Phase 5 — Web dashboard and service install (deferred)

The VEX doc next sends you to `JetsonWebDashboard/README.md` to install Node.js
and build the dashboard, then runs:

- `chmod +x service.sh run.sh`
- `sudo bash ./service.sh` (installs the `vexai` systemd unit for boot-time
  object detection)

**I'm leaving Phase 5 out of this plan deliberately.** Reasons:
1. The dashboard install steps live in a separate file we haven't reviewed.
2. The `service.sh` content depends on your fork — worth reading it before
   running as root.
3. Auto-start at boot should wait until we've confirmed the model loads and a
   RealSense camera is actually connected.

We'll do Phase 5 as a separate planning round after Phase 4 verifies clean.

---

## Phase 6 — Verification (after Phases 1–4)

Quick sanity script to run end-to-end:
```bash
python3 - <<'PY'
import sys, importlib
mods = ["numpy", "torch", "torchvision", "tensorrt", "pycuda.driver",
        "pyrealsense2", "cv2", "serial", "websocket_server", "PIL"]
for m in mods:
    try:
        x = importlib.import_module(m)
        v = getattr(x, "__version__", "?")
        print(f"OK    {m:20s} {v}")
    except Exception as e:
        print(f"FAIL  {m:20s} {e}")

import torch
print("torch.cuda.is_available():", torch.cuda.is_available())
print("torch.version.cuda:        ", torch.version.cuda)

import pycuda.driver as cuda
cuda.init()
print("pycuda devices:            ", cuda.Device.count())

import tensorrt as trt
print("tensorrt:                   ", trt.__version__)
PY
```

---

## Things explicitly NOT done in this plan

- **No nvpmodel change.** Staying at MODE_30W per your call (USB-C power until
  barrel jack arrives). Switch to MAXN later with
  `sudo nvpmodel -m 0 && sudo jetson_clocks`.
- **No NVMe setup.** eMMC only; monitor `df -h /` during the build.
- **No `clean.sh` / `power.sh`** from VEX's `Scripts/` folder. Those are Nano-
  specific (5 W power mode tuning, Nano-specific bloatware).
- **No TensorRT pip install.** System install is authoritative.
- **No `patch-realsense-ubuntu-lts.sh`.** Replaced by `FORCE_RSUSB_BACKEND=ON`.
- **No web dashboard / systemd service.** Deferred to a later round.

---

## Disk-space check before we start

eMMC was at 18 GB used / 36 GB free at survey time. Rough budget for what's
ahead:
- librealsense build tree: ~2 GB
- PyTorch + torchvision wheels + cache: ~3–4 GB
- pycuda + boost + build deps: ~500 MB
- VAIC repo + model files: depends on the fork
- apt cache, git objects, pip cache: ~1–2 GB

So ~7–10 GB consumed by this plan. Should leave us with ~25 GB free. Fine for
now, but we should watch it during Phase 5 (model artifacts can be large).
