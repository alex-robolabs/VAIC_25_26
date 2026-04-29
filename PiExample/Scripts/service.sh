#!/bin/bash
# Install and enable PiExample/Scripts/vexai.service.
# Idempotent — safe to re-run from deploy_pi.sh.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
SOURCE_UNIT="$SCRIPT_DIR/vexai.service"
TARGET_UNIT="/etc/systemd/system/vexai.service"

if [ ! -f "$SOURCE_UNIT" ]; then
    echo "ERROR: $SOURCE_UNIT not found" >&2
    exit 1
fi

echo "[service.sh] installing $TARGET_UNIT"
sudo install -m 0644 "$SOURCE_UNIT" "$TARGET_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable vexai
sudo systemctl restart vexai

echo "[service.sh] state: $(sudo systemctl is-active vexai)"
