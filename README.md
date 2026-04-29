# VAIC_25_26 — Robolabs fork

This is Robolabs' fork of VEX's `VAIC_25_26` reference repository for
the VEX AI Competition. It tracks
[VEX-Robotics/VAIC_25_26](https://github.com/VEX-Robotics/VAIC_25_26)
upstream and carries self-healing V5 ↔ companion-board comms layers
that make the reference architecture suitable for real development
loops rather than just match-day operation.

## Supported platforms

This fork supports both VEX-blessed AI companion boards. They live in
sibling directories and don't depend on each other:

- **Jetson Nano (legacy, frozen patch)** — the existing fleet (three
  fielded units running through the current season). Patch lives
  in `JetsonExample/`. No further development planned; bug fixes only.
- **Raspberry Pi 5 + Coral USB Accelerator (active development)** —
  the production target going forward. Cleaner internals, modern
  Python features, automated tests, structured state machine for
  link health. Lives in `PiExample/`.

The Pi work is a fresh design rather than a port of the Nano patch —
see **[`docs/pi-comms-design.md`](./docs/pi-comms-design.md)** for the
design doc and what changed.

## Quick start

**For a Pi (active platform):**

```bash
cd PiExample/Scripts
./bootstrap.sh <pi-ip> pi-<size>     # one-time per host
./deploy_pi.sh <pi-ip>               # ongoing deploys
```

Full guide: **[`PiExample/DEPLOY.md`](./PiExample/DEPLOY.md)**

**For an existing Jetson Nano:**

```bash
cd JetsonExample/Scripts
./deploy.sh <jetson-ip>
```

Full guide: **[`JetsonExample/DEPLOY.md`](./JetsonExample/DEPLOY.md)**

In both cases, the success signal is the same: walk to the robot and
check the V5 Brain's LCD for `Packets > 0` and increasing.

## Documentation

- **[`docs/comms-patch.md`](./docs/comms-patch.md)** — Nano-side comms
  patch technical writeup
- **[`docs/pi-comms-design.md`](./docs/pi-comms-design.md)** — Pi-side
  comms layer design doc (v2)
- **[`JetsonExample/DEPLOY.md`](./JetsonExample/DEPLOY.md)** — Nano
  deployment guide
- **[`PiExample/DEPLOY.md`](./PiExample/DEPLOY.md)** — Pi deployment
  guide

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

All patches and new code live in `JetsonExample/`, `PiExample/`, and
`docs/`. Upstream changes to `V5Example/`, `JetsonImages/`, and
`JetsonWebDashboard/` flow through cleanly.

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
