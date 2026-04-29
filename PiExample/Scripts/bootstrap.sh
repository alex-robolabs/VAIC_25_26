#!/bin/bash
# bootstrap.sh — one-time per-host setup for a Raspberry Pi joining the
# VEX AI fleet. Codifies the four steps from CLAUDE.md as an idempotent
# script so the second / third / Nth Pi doesn't require copy-paste from
# a runbook.
#
# Run from the Mac against a Pi that's networked but otherwise stock:
#
#   ./bootstrap.sh <target-ip> <new-hostname>
#   ./bootstrap.sh 10.0.0.20 pi-red
#   ./bootstrap.sh 10.0.0.21 pi-white
#
# Hostname must match the fleet pattern: 'pi-' followed by lowercase
# letters (pi-red, pi-white, pi-black, pi-gray, etc.). Continues the
# color convention from the Nano fleet (vex-red, vex-white).
#
# Steps performed (each is a no-op if already done):
#
#   1. ssh-copy-id: push the Mac's ed25519 public key. Skipped if the
#      key already authenticates non-interactively.
#
#   2. sudoers entry at /etc/sudoers.d/vexai permitting NOPASSWD on the
#      exact commands deploy_pi.sh + restart.sh need. Each grant is
#      narrow and justified:
#
#        /bin/systemctl       Start/stop/restart the vexai service
#                             (deploy_pi.sh, restart.sh, service.sh).
#
#        /usr/bin/journalctl  Tail the vexai journal during deploy
#                             verification (deploy_pi.sh).
#
#        /usr/bin/tee         Write /etc/systemd/system/vexai.service
#                             via 'sudo tee' (shell redirects can't be
#                             sudo'd directly), and write
#                             /sys/.../authorized in restart.sh's --usb
#                             mode to rebind V5 USB devices.
#
#        /usr/bin/pkill       Sweep stale pushback.py and 'serve -s
#                             build' processes that systemd might miss
#                             (restart.sh; backgrounded 'serve' from
#                             run.sh sometimes outlives a service stop).
#
#      Skipped if the file already contains exactly this line.
#
#   3. Hostname rename via hostnamectl. Skipped if already set.
#
#   4. Regenerate SSH host keys to break the cloned-image fingerprint
#      collision common in fresh Pi imager flashes. A marker file at
#      ~/.ssh/.vexai-bootstrap-done is written before the regeneration
#      so the step is idempotent even though the ssh session drops
#      mid-way through. After regeneration, the new ED25519 fingerprint
#      is printed for capture into CLAUDE.md's host-key table.

set -uo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <target-ip> <new-hostname>" >&2
    echo "       e.g. $0 10.0.0.20 pi-red" >&2
    exit 1
fi

TARGET_IP="$1"
NEW_HOSTNAME="$2"
TARGET_USER="${VEX_USER:-vex}"
SSH_TARGET="${TARGET_USER}@${TARGET_IP}"

# Hostname pattern: pi- followed by lowercase letters only.
if [[ ! "$NEW_HOSTNAME" =~ ^pi-[a-z]+$ ]]; then
    echo "ERROR: hostname must match pattern pi-[a-z]+ (lowercase letters only)" >&2
    echo "       got: $NEW_HOSTNAME" >&2
    exit 1
fi

# IPv4 sanity check (rough; doesn't validate octet ranges, just shape).
if [[ ! "$TARGET_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: target IP doesn't look like an IPv4 address: $TARGET_IP" >&2
    exit 1
fi

echo "[bootstrap] target:   $SSH_TARGET"
echo "[bootstrap] hostname: $NEW_HOSTNAME"
echo

echo "[bootstrap] checking reachability..."
if ! nc -z -w 3 "$TARGET_IP" 22 2>/dev/null; then
    echo "ERROR: $TARGET_IP:22 not reachable" >&2
    exit 1
fi

# ---------- step 1: ssh-copy-id ----------
echo "[bootstrap] step 1/4: ssh key auth"
PUBKEY="$HOME/.ssh/id_ed25519.pub"
if [ ! -f "$PUBKEY" ]; then
    echo "ERROR: no ed25519 public key at $PUBKEY" >&2
    echo "       generate one with: ssh-keygen -t ed25519" >&2
    exit 1
fi

if ssh -o BatchMode=yes -o ConnectTimeout=5 \
       -o StrictHostKeyChecking=accept-new \
       "$SSH_TARGET" true 2>/dev/null; then
    echo "  already authorized"
else
    echo "  authorizing $PUBKEY (one password prompt)"
    if ! ssh-copy-id -i "$PUBKEY" "$SSH_TARGET"; then
        echo "ERROR: ssh-copy-id failed" >&2
        exit 1
    fi
fi

# ---------- step 2: NOPASSWD sudoers ----------
echo "[bootstrap] step 2/4: sudoers entry"
SUDOERS_BODY="${TARGET_USER} ALL=(ALL) NOPASSWD: /bin/systemctl, /usr/bin/journalctl, /usr/bin/tee, /usr/bin/pkill"
SUDOERS_FILE='/etc/sudoers.d/vexai'

EXISTING=$(ssh "$SSH_TARGET" "sudo cat $SUDOERS_FILE 2>/dev/null || true")
# Strip trailing newline that 'cat' may leave for comparison.
EXISTING="${EXISTING%$'\n'}"
if [ "$EXISTING" = "$SUDOERS_BODY" ]; then
    echo "  already configured"
else
    echo "  installing $SUDOERS_FILE"
    ssh -t "$SSH_TARGET" "echo '${SUDOERS_BODY}' | sudo tee ${SUDOERS_FILE} >/dev/null && \
        sudo chmod 440 ${SUDOERS_FILE} && \
        sudo visudo -c -f ${SUDOERS_FILE}"
fi

# ---------- step 3: hostname rename ----------
echo "[bootstrap] step 3/4: hostname rename"
CURRENT_HOSTNAME=$(ssh "$SSH_TARGET" "hostname")
if [ "$CURRENT_HOSTNAME" = "$NEW_HOSTNAME" ]; then
    echo "  hostname already $NEW_HOSTNAME"
else
    echo "  renaming $CURRENT_HOSTNAME → $NEW_HOSTNAME"
    ssh -t "$SSH_TARGET" "sudo hostnamectl set-hostname ${NEW_HOSTNAME}"
fi

# /etc/hosts is fixed independently of the hostname rename. The stock
# Pi imager often writes a 127.0.1.1 line that doesn't match the
# original hostname (e.g. '127.0.1.1 vex' regardless of 'hostname'
# output), so a naive sed against $CURRENT_HOSTNAME wouldn't substitute.
# Without a correct entry, sudo prints 'unable to resolve host
# <new-hostname>' on every call. Cosmetic but annoying.
echo "  checking /etc/hosts for 127.0.1.1 → $NEW_HOSTNAME"
HOSTS_LINE=$(ssh "$SSH_TARGET" "grep -E '^127\.0\.1\.1[[:space:]]' /etc/hosts || true")
if echo "$HOSTS_LINE" | grep -qE "[[:space:]]${NEW_HOSTNAME}([[:space:]]|\$)"; then
    echo "    OK"
elif [ -z "$HOSTS_LINE" ]; then
    echo "    no 127.0.1.1 line; appending"
    ssh -t "$SSH_TARGET" "printf '127.0.1.1\\t${NEW_HOSTNAME}\\n' | sudo tee -a /etc/hosts >/dev/null"
else
    echo "    rewriting 127.0.1.1 line to include $NEW_HOSTNAME"
    ssh -t "$SSH_TARGET" "sudo sed -i.bak 's/^127\\.0\\.1\\.1.*/127.0.1.1\\t${NEW_HOSTNAME}/' /etc/hosts"
fi

# ---------- step 4: regenerate SSH host keys ----------
echo "[bootstrap] step 4/4: regenerate SSH host keys"
MARKER='$HOME/.ssh/.vexai-bootstrap-done'
if ssh "$SSH_TARGET" "test -f ${MARKER}"; then
    echo "  already regenerated (marker present)"
else
    echo "  regenerating /etc/ssh/ssh_host_*"
    # Write the marker BEFORE restarting ssh; the restart drops the
    # session, so anything chained after it won't run.
    ssh -t "$SSH_TARGET" "mkdir -p ~/.ssh && touch ${MARKER} && \
        sudo rm -f /etc/ssh/ssh_host_* && \
        sudo dpkg-reconfigure -f noninteractive openssh-server && \
        sudo systemctl restart ssh" || true

    # Mac-side known_hosts entry is now stale; clear it.
    ssh-keygen -R "$TARGET_IP" 2>/dev/null || true

    # Wait for sshd to come back up.
    sleep 3
    for _ in 1 2 3 4 5; do
        if nc -z -w 2 "$TARGET_IP" 22 2>/dev/null; then break; fi
        sleep 2
    done

    echo
    echo "  new ED25519 fingerprint:"
    ssh -o StrictHostKeyChecking=accept-new "$SSH_TARGET" \
        "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub" | sed 's/^/    /'
    echo
    echo "  ⚠️  capture this fingerprint into CLAUDE.md's SSH host key table"
fi

echo
echo "[bootstrap] done. $NEW_HOSTNAME ($TARGET_IP) is ready for:"
echo "    ./deploy_pi.sh $TARGET_IP"
