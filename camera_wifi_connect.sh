#!/usr/bin/env bash
set -euo pipefail

PROFILE="E-M5MKIII-P-BJ8A00203"
ALFA_OUI="00:c0:ca"

if [[ "${1:-}" != "connect" ]]; then
  echo "usage: $0 connect" >&2
  exit 2
fi

/usr/bin/nmcli radio wifi on

internal=""
backup=""
for device_path in /sys/class/net/wlan*; do
  [[ -e "$device_path" ]] || continue
  iface="${device_path##*/}"
  driver="$(basename "$(readlink -f "$device_path/device/driver" 2>/dev/null || true)")"
  mac="$(tr '[:upper:]' '[:lower:]' <"$device_path/address")"
  if [[ "$driver" == "brcmfmac" ]]; then
    internal="$iface"
  elif [[ "$mac" != "$ALFA_OUI"* ]]; then
    backup="$iface"
  fi
done

if [[ -z "$internal" ]]; then
  echo "Pi internal Wi-Fi was not found" >&2
  exit 1
fi

activate_camera() {
  local iface="$1"
  local mac
  mac="$(cat "/sys/class/net/$iface/address")"
  /usr/bin/nmcli connection modify "$PROFILE" \
    connection.interface-name "$iface" \
    802-11-wireless.mac-address "$mac" \
    ipv4.never-default yes \
    ipv6.never-default yes
  /usr/bin/nmcli connection up "$PROFILE" ifname "$iface"
}

if activate_camera "$internal"; then
  echo "Olympus camera connected on Pi internal Wi-Fi ($internal)"
  exit 0
fi

if [[ -n "$backup" ]] && activate_camera "$backup"; then
  echo "Internal Wi-Fi failed; Olympus camera connected on backup adapter ($backup)"
  exit 0
fi

echo "Olympus camera connection failed on internal and backup adapters" >&2
exit 1
