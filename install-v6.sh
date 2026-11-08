#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/home/timelapser/.config/timelapser"
CONFIG_FILE="$CONFIG_DIR/v6-hardware.env"
CAMERA_USB_HELPER="/usr/local/sbin/timelapser-camera-usb"

if [[ ! -f "$CONFIG_FILE" ]]; then
  install -d -m 0750 -o timelapser -g timelapser "$CONFIG_DIR"
  install -m 0640 -o timelapser -g timelapser "$ROOT/v6-hardware.env.example" "$CONFIG_FILE"
  echo "Created $CONFIG_FILE. Set both verified BCM GPIO numbers before enabling V6."
  exit 2
fi

if grep -Eq '^TIMELAPSER_V6_(FOCUS|SHUTTER)_GPIO=$' "$CONFIG_FILE"; then
  echo "Refusing installation: both relay GPIO assignments must be filled in $CONFIG_FILE" >&2
  exit 2
fi

"/home/timelapser/timelapser-venv/bin/pip" install -r "$ROOT/requirements_v5.txt"
install -m 0755 "$ROOT/camera_usb_port.py" "$CAMERA_USB_HELPER"
install -m 0644 "$ROOT/timelapser-camera-release.service" /etc/systemd/system/
install -m 0644 "$ROOT/timelapser-director-v6.service" /etc/systemd/system/
install -m 0644 "$ROOT/timelapser-scheduler-v6.service" /etc/systemd/system/

systemctl disable --now timelapser-scheduler.service timelapser-director.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable timelapser-camera-release.service
systemctl restart timelapser-camera-release.service
systemctl enable --now timelapser-scheduler-v6.service timelapser-director-v6.service

echo "Timelapser V6 services installed."
systemctl --no-pager --full status timelapser-camera-release.service \
  timelapser-scheduler-v6.service timelapser-director-v6.service || true

