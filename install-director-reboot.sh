#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"
HELPER="/usr/local/sbin/timelapser-reboot"
SUDOERS="/etc/sudoers.d/timelapser-director-reboot"

install -o root -g root -m 0755 "$ROOT/timelapser-reboot.sh" "$HELPER"
cat >"$SUDOERS" <<EOF
roy ALL=(root) NOPASSWD: $HELPER request, $HELPER force
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS"

"$HELPER" check
sudo -u roy sudo -n -l "$HELPER" request "$HELPER" force >/dev/null
systemctl restart timelapser-director.service
echo "Director reboot helper installed and permissions verified."
