#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

sudo install -m 0644 "$ROOT/timelapser-environment.service" /etc/systemd/system/timelapser-environment.service
sudo install -m 0644 "$ROOT/timelapser-environment.timer" /etc/systemd/system/timelapser-environment.timer
sudo systemctl daemon-reload
sudo systemctl enable --now timelapser-environment.timer
sudo systemctl start timelapser-environment.service
sudo systemctl --no-pager status timelapser-environment.timer
