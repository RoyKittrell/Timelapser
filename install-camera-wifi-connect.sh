#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
HELPER="/usr/local/sbin/timelapser-camera-wifi"
SUDOERS="/etc/sudoers.d/timelapser-camera-wifi"
PROFILE="E-M5MKIII-P-BJ8A00203"

install -o root -g root -m 0755 "$ROOT/camera_wifi_connect.sh" "$HELPER"

# The Olympus profile is bound to the dedicated adapter by permanent MAC
# address. Do not also pin it to a volatile wlan number: those names can change
# when USB radios are moved between ports or detected in a different order.
/usr/bin/nmcli connection modify "$PROFILE" \
  connection.autoconnect yes \
  connection.autoconnect-retries 0 \
  connection.interface-name "" \
  ipv4.never-default yes \
  ipv6.never-default yes

cat >"$SUDOERS" <<EOF
roy ALL=(root) NOPASSWD: $HELPER connect
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS"

"$HELPER" connect
sudo -u roy sudo -n "$HELPER" connect >/dev/null
/usr/bin/systemctl restart timelapser-scheduler.service

echo "Camera Wi-Fi auto-connect helper installed and tested."
