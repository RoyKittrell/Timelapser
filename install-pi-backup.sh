#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"

install -o root -g root -m 0755 "$ROOT/pi-nightly-backup.sh" /usr/local/sbin/timelapser-nightly-backup
install -o root -g root -m 0644 "$ROOT/timelapser-backup.service" /etc/systemd/system/timelapser-backup.service
install -o root -g root -m 0644 "$ROOT/timelapser-backup.timer" /etc/systemd/system/timelapser-backup.timer
systemctl daemon-reload
systemctl enable --now timelapser-backup.timer
/usr/local/sbin/timelapser-nightly-backup --check
systemctl --no-pager --full status timelapser-backup.timer

