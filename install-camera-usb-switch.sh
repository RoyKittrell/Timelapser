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

# Record the presently connected Olympus port before cutting its power.
"$HELPER" discover
sync
camera_disk=$(lsblk -dnpo PATH,MODEL | awk '$0 ~ /E-M5MarkIII/ {print $1; exit}')
if [[ -n "${camera_disk:-}" ]]; then
  while read -r partition kind; do
    if [[ "$kind" == "part" ]] && findmnt -rn -S "$partition" >/dev/null; then
      umount "$partition"
    fi
  done < <(lsblk -nrpo PATH,TYPE "$camera_disk")
fi

install -o root -g root -m 0644 "$ROOT/timelapser-scheduler.service" /etc/systemd/system/timelapser-scheduler.service
systemctl daemon-reload
systemctl restart timelapser-scheduler.service
systemctl --no-pager --full status timelapser-scheduler.service
cat /var/lib/timelapser/camera_usb_port.json
