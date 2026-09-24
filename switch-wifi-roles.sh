#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
HOME_SOURCE="netplan-wlan0-_A29-01"
HOME_INTERNAL="timelapser-home-internal"
CAMERA="E-M5MKIII-P-BJ8A00203"

if ! nmcli -t -f DEVICE,STATE device status | grep -qx 'wlan1:connected'; then
  echo "Safety check failed: the existing wlan1 home connection is not active." >&2
  exit 1
fi

if ! nmcli -g NAME connection show | grep -Fxq "$HOME_INTERNAL"; then
  nmcli connection clone "$HOME_SOURCE" "$HOME_INTERNAL"
fi
nmcli connection modify "$HOME_INTERNAL" \
  connection.interface-name wlan0 \
  connection.autoconnect yes \
  connection.autoconnect-priority 100 \
  802-11-wireless.band a \
  ipv4.never-default no

nmcli connection down "$CAMERA" 2>/dev/null || true
nmcli connection up "$HOME_INTERNAL" ifname wlan0

if [[ "$(nmcli -g GENERAL.STATE device show wlan0)" != 100* ]]; then
  echo "Safety check failed: wlan0 did not establish the home connection." >&2
  exit 1
fi

nmcli connection modify "$HOME_SOURCE" connection.autoconnect no
nmcli connection modify "$CAMERA" \
  connection.interface-name wlan1 \
  connection.autoconnect yes \
  connection.autoconnect-retries 0 \
  ipv4.never-default yes

install -o root -g root -m 0755 "$ROOT/camera_wifi_connect.sh" \
  /usr/local/sbin/timelapser-camera-wifi

nmcli connection up "$CAMERA" ifname wlan1

echo "Wi-Fi roles switched: wlan0=home 5GHz, wlan1=Olympus camera."
nmcli -t -f DEVICE,STATE,CONNECTION device status
