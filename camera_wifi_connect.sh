#!/usr/bin/env bash
set -euo pipefail

PROFILE="E-M5MKIII-P-BJ8A00203"
INTERFACE="$(/usr/bin/nmcli -g connection.interface-name connection show "$PROFILE" 2>/dev/null || true)"
INTERFACE="${INTERFACE:-wlan0}"

if [[ "${1:-}" != "connect" ]]; then
  echo "usage: $0 connect" >&2
  exit 2
fi

/usr/bin/nmcli radio wifi on
/usr/bin/nmcli connection up "$PROFILE" ifname "$INTERFACE"
