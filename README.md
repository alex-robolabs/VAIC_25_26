# VAIC_25_26 — Robolabs fork

This is Robolabs' fork of VEX's `VAIC_25_26` reference repository for
the VEX AI Competition. It tracks
[VEX-Robotics/VAIC_25_26](https://github.com/VEX-Robotics/VAIC_25_26)
upstream and carries a self-healing V5 ↔ Jetson comms patch that
makes the reference architecture suitable for real development loops
rather than just match-day operation.

## What's different from upstream

The Jetson-side serial connection manager has been rewritten to
reconnect automatically after V5 reprograms, battery swaps, USB drops,
and silent stalls. The wire protocol is unchanged — no V5 brain
firmware changes are required.

- Technical writeup: **[`docs/comms-patch.md`](./docs/comms-patch.md)**
- Deployment guide: **[`JetsonExample/DEPLOY.md`](./JetsonExample/DEPLOY.md)**

If you're picking up a Jetson that's already running the patch, the
success signal is simple: check the V5 Brain's LCD dashboard for
`Packets > 0` and increasing.

## Quick start

Deploy to one Jetson from your Mac:

```bash
cd JetsonExample/Scripts
./deploy.sh <jetson-ip>
```

Then look at the V5 Brain's LCD — that's the ground truth. See
`DEPLOY.md` for prereqs, SSH-key setup, troubleshooting, and the
alternative deploy paths (USB stick, manual scp).

## Upstream tracking

```
origin    https://github.com/alex-robolabs/VAIC_25_26
upstream  https://github.com/VEX-Robotics/VAIC_25_26
```

To pull upstream changes:

```bash
git fetch upstream
git merge upstream/main
```

The patch lives entirely in `JetsonExample/` and `docs/`, so upstream
merges should stay clean.

---

## Original VEX README

The original VEX-authored README content is preserved below for
reference.

---

# The VEX AI Competition (VAIC) System

## [JetsonExample](./JetsonExample/README.md)

JetsonExample contains the default code that powers the VEX AI system, from processing image data and running the AI model to detect objects for the VEX V5 Brain.

## [JetsonImages](./JetsonImages/README.md)

JetsonImages is where you will find how to get the most up-to-date image of the NVIDIA Jetson Nano and Raspberry Pi 5 and instructions on how to install the SD card image and or building from source.

## [JetsonWebDashboard](./JetsonWebDashboard/README.md)

JetsonWebDashboard is where you will find the source code for the VEX AI Web Dashboard that runs on the Jetson Nano/Raspberry Pi.

## [V5Example](./V5Example/ai_demo/README.md)

V5Example contains the `ai_demo` V5 Project which has examples on how to connect with the Jetson Nano/Raspberry Pi and how to interpret and process the data from the board on the V5 Brain
