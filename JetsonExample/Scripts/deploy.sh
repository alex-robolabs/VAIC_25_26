#!/bin/bash
# deploy.sh — push the self-healing comms patch to one Jetson.
#
# Uses SSH ControlMaster to share a single authenticated session across
# all ssh/scp operations, so you're prompted for the SSH password at
# most ONCE per deploy. Sudo on the remote may prompt one additional
# time (unless the user has NOPASSWD configured).
#
# Usage:
#   ./deploy.sh <jetson-host>              (user defaults to "vex")
#   ./deploy.sh <jetson-host> <user>
#
# To deploy to all four Jetsons:
#   for h in 192.168.1.10 192.168.1.11 192.168.1.12 192.168.1.13; do
#       ./deploy.sh $h
#   done
#
# Prereqs on the Jetson:
#   - Repo already at ~/VAIC_25_26 (adjust REMOTE_BASE below if different)
#   - Remote user owns ~/VAIC_25_26/JetsonExample/ or has read/write
#     access to it (the script writes .py files and the tar backup there)
#   - User can sudo (NOPASSWD strongly recommended — see DEPLOY.md)
#   - Strongly recommended: ssh-key auth (see DEPLOY.md)

set -uo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <jetson-host-or-ip> [user]" >&2
    echo "       e.g. $0 192.168.1.10 vex" >&2
    exit 1
fi

HOST="$1"
REMOTE_USER="${2:-vex}"
TARGET="${REMOTE_USER}@${HOST}"
REMOTE_BASE="~/VAIC_25_26/JetsonExample"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

# All ssh/scp ops share one multiplexed connection. The first op opens
# the master (and prompts for ssh password if no key auth); subsequent
# ops reuse the unix socket without re-authenticating.
CTL_SOCK="/tmp/vexai-deploy.${HOST}.$$.sock"
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

echo "[deploy] target:     $TARGET"
echo "[deploy] remote dir: $REMOTE_BASE"
echo "[deploy] source dir: $SOURCE_DIR"
echo

echo "[deploy] opening ssh session (one-time password prompt if no key auth)..."
if ! ssh "${SSH_OPTS[@]}" -o ConnectTimeout=5 "$TARGET" true; then
    echo "[deploy] ERROR: cannot reach $TARGET over ssh" >&2
    exit 1
fi

echo "[deploy] backing up originals on remote..."
ssh "${SSH_OPTS[@]}" "$TARGET" "cd $REMOTE_BASE && \
    tar czf ~/vexai-backup-\$(date +%Y%m%d-%H%M%S).tgz \
        V5Comm.py V5Position.py pushback.py 2>/dev/null || true"

echo "[deploy] copying Python files..."
scp "${SSH_OPTS[@]}" \
    "$SOURCE_DIR/serial_link.py" \
    "$SOURCE_DIR/vexai_logging.py" \
    "$SOURCE_DIR/V5Comm.py" \
    "$SOURCE_DIR/V5Position.py" \
    "$SOURCE_DIR/pushback.py" \
    "$SOURCE_DIR/show_ports.py" \
    "$TARGET:$REMOTE_BASE/"

echo "[deploy] copying restart.sh..."
scp "${SSH_OPTS[@]}" "$SCRIPT_DIR/restart.sh" "$TARGET:$REMOTE_BASE/Scripts/"

echo "[deploy] running restart + verify on remote (one sudo prompt unless NOPASSWD)..."
# -t forces a pty so sudo can prompt cleanly. All sudo calls run inside
# this single ssh session, so sudo's credential cache covers them.
ssh -t "${SSH_OPTS[@]}" "$TARGET" "
    set -e
    chmod +x $REMOTE_BASE/Scripts/restart.sh
    sudo systemctl restart vexai
    sleep 3
    sudo systemctl status vexai --no-pager | head -8
    echo
    echo '[remote] waiting 15s for service to finish initializing (TFLite + RealSense imports take a moment)...'
    sleep 15
    echo
    echo '[remote] checking journal for successful link connection...'
    sudo journalctl -u vexai --since '20 sec ago' --no-pager | \
        grep -E 'connected on|opening|watchdog tripped|ERROR' | tail -10
"

echo
echo "[deploy] ssh phase complete."
echo
echo "*** IMPORTANT: now confirm on the V5 Brain LCD dashboard. ***"
echo
echo "  Success signal: 'Packets' counter is non-zero AND increasing."
echo "  (Service active is NOT enough — the Jetson logs can show data=True"
echo "   while the V5 sees zero bytes if the wrong USB port is selected.)"
echo
echo "  If Packets stays at 0, run the descriptor helper on the Jetson:"
echo
echo "      ssh $TARGET 'python3 ~/VAIC_25_26/JetsonExample/show_ports.py'"
echo
echo "  The V5 Brain User Port must be present (V5 must be powered on AND"
echo "  running ai_demo for that endpoint to enumerate). If it's missing,"
echo "  this is your problem — not a Jetson-side bug."
echo
echo "[deploy] tail logs:  ssh $TARGET 'sudo journalctl -u vexai -f'"
echo "[deploy] rollback:   ssh $TARGET 'cd $REMOTE_BASE && tar xzf ~/vexai-backup-*.tgz && sudo systemctl restart vexai'"
