#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${TIMELAPSER_SERVICE_USER:-timelapser}"
HELPER="/usr/local/sbin/timelapser-camera-usb"
SUDOERS="/etc/sudoers.d/timelapser-camera-usb"

install -o root -g root -m 0755 "$ROOT/camera_usb_port.py" "$HELPER"
cat >"$SUDOERS" <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: $HELPER discover, $HELPER discover-and-off, $HELPER on, $HELPER off
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS"

# Discover, unmount only Olympus storage, sync, then cut the saved data port.
"$HELPER" discover-and-off

install -o root -g root -m 0644 "$ROOT/timelapser-scheduler.service" /etc/systemd/system/timelapser-scheduler.service
systemctl daemon-reload
systemctl restart timelapser-scheduler.service
systemctl --no-pager --full status timelapser-scheduler.service
cat /var/lib/timelapser/camera_usb_port.json
