#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
HELPER="/usr/local/sbin/timelapser-camera-usb"
SUDOERS="/etc/sudoers.d/timelapser-camera-usb"

install -o root -g root -m 0755 "$ROOT/camera_usb_port.py" "$HELPER"
cat >"$SUDOERS" <<EOF
roy ALL=(root) NOPASSWD: $HELPER discover, $HELPER discover-and-off, $HELPER on, $HELPER off
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS"

# Discover, safely unmount, sync, and disconnect when the camera is visible.
# The helper deliberately succeeds when the port is already off, allowing
# installation and service recovery without reconnecting the camera first.
"$HELPER" discover-and-off

install -o root -g root -m 0644 "$ROOT/timelapser-scheduler.service" /etc/systemd/system/timelapser-scheduler.service
systemctl daemon-reload
systemctl restart timelapser-scheduler.service
systemctl --no-pager --full status timelapser-scheduler.service
cat /var/lib/timelapser/camera_usb_port.json
