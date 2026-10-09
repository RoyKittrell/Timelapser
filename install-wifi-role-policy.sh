#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
HOME_SOURCE="netplan-wlan0-_A29-01"
HOME_ALFA="Timelapser Home ALFA"
CAMERA_PROFILE="E-M5MKIII-P-BJ8A00203"
HELPER="/usr/local/sbin/timelapser-camera-wifi"

if [[ $EUID -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

internal=""
alfa=""
backup=""
for device_path in /sys/class/net/wlan*; do
  [[ -e "$device_path" ]] || continue
  iface="${device_path##*/}"
  driver="$(basename "$(readlink -f "$device_path/device/driver" 2>/dev/null || true)")"
  mac="$(tr '[:upper:]' '[:lower:]' <"$device_path/address")"
  if [[ "$driver" == "brcmfmac" ]]; then
    internal="$iface"
  elif [[ "$mac" == 00:c0:ca:* ]]; then
    alfa="$iface"
  else
    backup="$iface"
  fi
done

if [[ -z "$internal" || -z "$alfa" || -z "$backup" ]]; then
  echo "Could not identify all radios: internal=$internal alfa=$alfa backup=$backup" >&2
  exit 1
fi

if ! nmcli -g NAME connection show | grep -Fxq "$HOME_ALFA"; then
  nmcli connection clone "$HOME_SOURCE" "$HOME_ALFA"
fi

alfa_mac="$(cat "/sys/class/net/$alfa/address")"
nmcli connection modify "$HOME_ALFA" \
  connection.interface-name "$alfa" \
  802-11-wireless.mac-address "$alfa_mac" \
  connection.autoconnect yes \
  connection.autoconnect-priority 200 \
  ipv4.method auto \
  ipv4.addresses "" \
  ipv4.gateway "" \
  ipv4.dns "" \
  ipv4.route-metric 50 \
  ipv4.never-default no \
  ipv6.never-default no

# Move ALFA from any old camera session to the home network before releasing
# the internal radio. The current internal home connection remains up until
# the camera helper switches it, avoiding a management-network gap.
nmcli connection down "$CAMERA_PROFILE" 2>/dev/null || true
nmcli connection up "$HOME_ALFA" ifname "$alfa"
if [[ "$(nmcli -g GENERAL.STATE device show "$alfa")" != 100* ]]; then
  echo "ALFA failed to establish the home connection; leaving internal home active." >&2
  exit 1
fi

internal_mac="$(cat "/sys/class/net/$internal/address")"
nmcli connection modify "$CAMERA_PROFILE" \
  connection.interface-name "$internal" \
  802-11-wireless.mac-address "$internal_mac" \
  connection.autoconnect yes \
  connection.autoconnect-priority 100 \
  ipv4.never-default yes \
  ipv6.never-default yes

install -o root -g root -m 0755 "$ROOT/camera_wifi_connect.sh" "$HELPER"

# Prevent the internal radio from reclaiming home Wi-Fi on subsequent boots.
# These are cleanup operations rather than prerequisites, so report a warning
# instead of abandoning an otherwise valid role configuration.
nmcli connection modify "$HOME_SOURCE" connection.autoconnect no || \
  echo "Warning: could not disable autoconnect on $HOME_SOURCE" >&2
if nmcli -g NAME connection show | grep -Fxq "Timelapser Home Internal"; then
  nmcli connection modify "Timelapser Home Internal" connection.autoconnect no || \
    echo "Warning: could not disable Timelapser Home Internal autoconnect" >&2
fi

echo "Wi-Fi roles configured:"
echo "  home:   ALFA ($alfa)"
echo "  camera: Pi internal ($internal)"
echo "  backup: small USB adapter ($backup)"
nmcli -t -f DEVICE,STATE,CONNECTION device status
