#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
HOME_PROFILE="netplan-wlan0-_A29-01"
CAMERA_PROFILE="E-M5MKIII-P-BJ8A00203"
HOME_MAC="00:C0:CA:B8:4C:EE"
CAMERA_MAC="C0:3A:55:A1:1B:D6"
DRIVE_UUID="C6BEB888BEB87293"

nmcli connection modify "$HOME_PROFILE" \
  connection.interface-name "" \
  802-11-wireless.mac-address "$HOME_MAC" \
  connection.autoconnect yes

nmcli connection modify "$CAMERA_PROFILE" \
  connection.interface-name "" \
  802-11-wireless.mac-address "$CAMERA_MAC" \
  connection.autoconnect yes \
  connection.autoconnect-retries 0 \
  ipv4.never-default yes

install -o root -g root -m 0755 "$ROOT/camera_wifi_connect.sh" \
  /usr/local/sbin/timelapser-camera-wifi

drive_path="$(findfs "UUID=$DRIVE_UUID" 2>/dev/null || true)"
if [[ -z "$drive_path" ]]; then
  echo "WARNING: Timelapser drive UUID $DRIVE_UUID is not currently attached." >&2
else
  echo "Timelapser drive discovered as $drive_path (USB port independent)."
fi

echo "Home profile bound to adapter MAC $HOME_MAC."
echo "Camera profile bound to adapter MAC $CAMERA_MAC."
echo "Camera USB switching already discovers the Olympus VID/PID and current hub port."
nmcli -t -f NAME,DEVICE connection show --active
