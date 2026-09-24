#!/usr/bin/env bash
set -euo pipefail

PROFILE="E-M5MKIII-P-BJ8A00203"

if [[ "${1:-}" != "connect" ]]; then
  echo "usage: $0 connect" >&2
  exit 2
fi

/usr/bin/nmcli radio wifi on
/usr/bin/nmcli connection up "$PROFILE"
