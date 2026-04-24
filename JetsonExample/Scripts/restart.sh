#!/bin/bash
# restart.sh — clean stop+start of the VEX AI pipeline without rebooting.
#
# Usage:
#   ./restart.sh                 Clean restart of the vexai service.
#   ./restart.sh --usb           Also rebind all V5 USB devices (VID 2888).
#                                Use this if the serial port is wedged and
#                                a normal restart doesn't bring the link back.
#   ./restart.sh --logs          Follow journalctl after starting.
#   ./restart.sh --usb --logs    Both.
#
# Exit codes:
#   0  Success
#   1  systemctl restart failed
#   2  Invalid argument

set -uo pipefail

REBIND_USB=false
TAIL_LOGS=false

for arg in "$@"; do
    case "$arg" in
        --usb)  REBIND_USB=true ;;
        --logs) TAIL_LOGS=true ;;
        -h|--help)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2
            ;;
    esac
done

echo "[restart] stopping vexai service..."
sudo systemctl stop vexai

echo "[restart] sweeping any leftover processes..."
sudo pkill -f pushback.py 2>/dev/null || true
sudo pkill -f 'serve -s build' 2>/dev/null || true
sleep 1

if $REBIND_USB; then
    echo "[restart] rebinding V5 USB devices (VID 2888)..."
    FOUND=0
    for vendor_file in /sys/bus/usb/devices/*/idVendor; do
        if [[ "$(cat "$vendor_file" 2>/dev/null)" == "2888" ]]; then
            dev_dir="$(dirname "$vendor_file")"
            echo "  resetting $(basename "$dev_dir")"
            echo 0 | sudo tee "$dev_dir/authorized" > /dev/null
            sleep 0.5
            echo 1 | sudo tee "$dev_dir/authorized" > /dev/null
            FOUND=$((FOUND + 1))
        fi
    done
    if [[ $FOUND -eq 0 ]]; then
        echo "  (no V5 devices currently enumerated; skipping rebind)"
    else
        echo "  rebound $FOUND device(s); giving kernel 2s to re-enumerate..."
        sleep 2
    fi
fi

echo "[restart] starting vexai service..."
if ! sudo systemctl start vexai; then
    echo "[restart] ERROR: systemctl start failed" >&2
    exit 1
fi
sleep 1

echo
sudo systemctl status vexai --no-pager | head -10
echo

if $TAIL_LOGS; then
    echo "[restart] following logs (Ctrl-C to exit)..."
    sudo journalctl -u vexai -f
else
    echo "[restart] tail logs with: sudo journalctl -u vexai -f"
fi
