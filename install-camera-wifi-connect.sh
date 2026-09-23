#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
HELPER="/usr/local/sbin/timelapser-camera-wifi"
SUDOERS="/etc/sudoers.d/timelapser-camera-wifi"
PROFILE="E-M5MKIII-P-BJ8A00203"

install -o root -g root -m 0755 "$ROOT/camera_wifi_connect.sh" "$HELPER"

# Keep internet routing on the external/home adapter while wlan0 talks only to
# the camera. Retry indefinitely whenever the Olympus SSID becomes available.
/usr/bin/nmcli connection modify "$PROFILE" \
  connection.autoconnect yes \
  connection.autoconnect-retries 0 \
  connection.interface-name wlan0 \
  ipv4.never-default yes \
  ipv6.never-default yes

cat >"$SUDOERS" <<EOF
roy ALL=(root) NOPASSWD: $HELPER connect
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS"

"$HELPER" connect
sudo -u roy sudo -n "$HELPER" connect >/dev/null

echo "Camera Wi-Fi auto-connect helper installed and tested."
