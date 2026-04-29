#!/bin/bash
# Pi-side launcher for the vexai service.
#
# Sets PYTHONPATH so PiExample's redesigned comms layer takes priority
# over the upstream stock files in JetsonExample/. Other modules
# (V5Web, V5MapPosition, model, model_backend, data_processing) are
# sourced from JetsonExample/ — they're outside the comms-patch scope
# and run unchanged on Pi.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
PIEXAMPLE_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$PIEXAMPLE_DIR")"
JETSONEXAMPLE_DIR="$REPO_ROOT/JetsonExample"
DASHBOARD_DIR="$REPO_ROOT/JetsonWebDashboard/vexai-web-dashboard-react"

# pyenv shim path: VEX's Pi install pins Python to 3.9 via pyenv
# (pycoral does not yet build on 3.10+).
export PATH="$HOME/.pyenv/shims:$PATH"

# PiExample first so our V5Comm.py / V5Position.py / serial_link.py
# shadow the upstream stock files at import time.
export PYTHONPATH="$PIEXAMPLE_DIR:$JETSONEXAMPLE_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Web dashboard (background). 'serve' is installed by VEX's Pi setup.
if [ -d "$DASHBOARD_DIR/build" ]; then
    cd "$DASHBOARD_DIR"
    serve -s build &
fi

cd "$PIEXAMPLE_DIR"
exec python3 pushback.py
