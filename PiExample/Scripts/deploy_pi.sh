#!/bin/bash
# deploy_pi.sh — push the PiExample comms layer to one Raspberry Pi.
#
# Adapted from JetsonExample/Scripts/deploy.sh. Differences for Pi:
#   - Targets ~/VAIC_25_26/PiExample/ on the remote
#   - No vexai-fan.service (Pi 5 kernel governor handles cooling)
#   - Uses static vexai.service from PiExample/Scripts/
#   - Restarts vexai and tails the journal for verification
#
# Uses SSH ControlMaster so you authenticate at most once per deploy.
#
# Usage:
#   ./deploy_pi.sh <pi-host>              (user defaults to "vex")
#   ./deploy_pi.sh <pi-host> <user>
#
# Prereqs on the Pi (run once via PiExample/Scripts/bootstrap.sh):
#   - SSH key auth set up
#   - NOPASSWD sudo for /bin/systemctl, /usr/bin/journalctl,
#     /usr/bin/tee, /usr/bin/pkill (see bootstrap.sh)
#   - Hostname renamed to fleet convention (pi-red, pi-white, etc.)
#   - Repo cloned to ~/VAIC_25_26 (with at least JetsonExample/ —
#     PiExample/ will be created by this script if missing)

set -uo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <pi-host-or-ip> [user]" >&2
    echo "       e.g. $0 10.0.0.20 vex" >&2
    exit 1
fi

HOST="$1"
REMOTE_USER="${2:-vex}"
TARGET="${REMOTE_USER}@${HOST}"
REMOTE_BASE="\$HOME/VAIC_25_26/PiExample"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

CTL_SOCK="/tmp/vexai-pi-deploy.${HOST}.$$.sock"
SSH_OPTS=(
    -o "ControlMaster=auto"
    -o "ControlPath=${CTL_SOCK}"
    -o "ControlPersist=60s"
    -o "StrictHostKeyChecking=accept-new"
)

cleanup() {
    ssh "${SSH_OPTS[@]}" -O exit "$TARGET" 2>/dev/null || true
}
trap cleanup EXIT

echo "[deploy_pi] target:     $TARGET"
echo "[deploy_pi] remote dir: $REMOTE_BASE"
echo "[deploy_pi] source dir: $SOURCE_DIR"
echo

echo "[deploy_pi] opening ssh session..."
if ! ssh "${SSH_OPTS[@]}" -o ConnectTimeout=5 "$TARGET" true; then
    echo "[deploy_pi] ERROR: cannot reach $TARGET over ssh" >&2
    exit 1
fi

echo "[deploy_pi] preparing remote directory..."
ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p ~/VAIC_25_26/PiExample/Scripts ~/VAIC_25_26/PiExample/tests/unit"

echo "[deploy_pi] backing up any existing PiExample comms files..."
ssh "${SSH_OPTS[@]}" "$TARGET" "cd ~/VAIC_25_26/PiExample && \
    tar czf ~/vexai-pi-backup-\$(date +%Y%m%d-%H%M%S).tgz \
        V5Comm.py V5Position.py serial_link.py link_stats.py \
        vexai_logging.py pushback.py 2>/dev/null || true"

echo "[deploy_pi] copying Python files..."
scp "${SSH_OPTS[@]}" \
    "$SOURCE_DIR/V5Comm.py" \
    "$SOURCE_DIR/V5Position.py" \
    "$SOURCE_DIR/serial_link.py" \
    "$SOURCE_DIR/link_stats.py" \
    "$SOURCE_DIR/vexai_logging.py" \
    "$SOURCE_DIR/filter.py" \
    "$SOURCE_DIR/pushback.py" \
    "$SOURCE_DIR/show_ports.py" \
    "$SOURCE_DIR/__init__.py" \
    "$TARGET:~/VAIC_25_26/PiExample/"

echo "[deploy_pi] copying Scripts..."
scp "${SSH_OPTS[@]}" \
    "$SCRIPT_DIR/run.sh" \
    "$SCRIPT_DIR/service.sh" \
    "$SCRIPT_DIR/restart.sh" \
    "$SCRIPT_DIR/vexai.service" \
    "$TARGET:~/VAIC_25_26/PiExample/Scripts/"

echo "[deploy_pi] copying tests..."
scp "${SSH_OPTS[@]}" \
    "$SOURCE_DIR/tests/conftest.py" \
    "$SOURCE_DIR/tests/__init__.py" \
    "$TARGET:~/VAIC_25_26/PiExample/tests/"
scp "${SSH_OPTS[@]}" \
    "$SOURCE_DIR/tests/unit/__init__.py" \
    "$SOURCE_DIR/tests/unit/test_protocol_parsing.py" \
    "$SOURCE_DIR/tests/unit/test_link_state_machine.py" \
    "$TARGET:~/VAIC_25_26/PiExample/tests/unit/"

echo "[deploy_pi] installing systemd unit + restarting vexai..."
ssh -t "${SSH_OPTS[@]}" "$TARGET" "
    set -e
    chmod +x ~/VAIC_25_26/PiExample/Scripts/run.sh
    chmod +x ~/VAIC_25_26/PiExample/Scripts/service.sh
    chmod +x ~/VAIC_25_26/PiExample/Scripts/restart.sh
    chmod +x ~/VAIC_25_26/PiExample/Scripts/deploy_pi.sh 2>/dev/null || true

    sudo install -m 0644 ~/VAIC_25_26/PiExample/Scripts/vexai.service \
        /etc/systemd/system/vexai.service
    sudo systemctl daemon-reload
    sudo systemctl enable vexai
    sudo systemctl restart vexai
    sleep 3
    sudo systemctl status vexai --no-pager | head -8

    echo
    echo '[remote] waiting 15s for service to finish initializing...'
    sleep 15
    echo
    echo '[remote] checking journal for link activity...'
    sudo journalctl -u vexai --since '20 sec ago' --no-pager | \
        grep -E 'opening|HALF_OPEN|OPERATING|reconnect|ERROR|first bytes' | tail -15
"

echo
echo "[deploy_pi] ssh phase complete."
echo
echo "*** IMPORTANT: confirm on the V5 Brain LCD dashboard. ***"
echo
echo "  Success signal: 'Packets' counter is non-zero AND increasing."
echo "  Pi-side OPERATING state is necessary but NOT sufficient."
echo
echo "  If Packets stays at 0, run the descriptor helper:"
echo "      ssh $TARGET 'python3 ~/VAIC_25_26/PiExample/show_ports.py'"
echo
echo "[deploy_pi] tail logs:  ssh $TARGET 'sudo journalctl -u vexai -f'"
echo "[deploy_pi] rollback:   ssh $TARGET 'cd ~/VAIC_25_26/PiExample && tar xzf ~/vexai-pi-backup-*.tgz && sudo systemctl restart vexai'"
