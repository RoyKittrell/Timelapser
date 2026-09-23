#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/roy/Timelapser Sept2026/timelapser_v5"

# Keep the camera on the internal adapter and the home network on USB Wi-Fi.
nmcli connection modify netplan-wlan0-_A29-01 \
  connection.interface-name wlan1 connection.autoconnect yes connection.autoconnect-priority 100 \
  802-11-wireless.powersave 2
nmcli connection modify E-M5MKIII-P-BJ8A00203 \
  connection.interface-name wlan0 connection.autoconnect yes connection.autoconnect-priority 50

install -m 0644 "$ROOT/timelapser-director.service" /etc/systemd/system/timelapser-director.service

# Preserve the previous boot's diagnostics if connectivity drops again.
mkdir -p /var/log/journal
systemctl restart systemd-journald
systemctl daemon-reload
systemctl enable --now timelapser-director.service

systemctl --no-pager --full status timelapser-director.service
nmcli -f NAME,DEVICE,AUTOCONNECT,AUTOCONNECT-PRIORITY connection show
