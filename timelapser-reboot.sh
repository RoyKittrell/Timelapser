#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"
case "$mode" in
  check|request|force) ;;
  *) echo "Usage: $0 {check|request|force}" >&2; exit 2 ;;
esac

if systemctl list-jobs --no-legend 2>/dev/null | grep -Eq 'reboot\.target|systemd-reboot'; then
  echo "A reboot is already scheduled."
  exit 4
fi

busy_pattern='/timelapser_v5(_aperture_priority)?\.py|/timelapser_postprocess_v5\.py|/download_run_fullres\.py|/smooth_exposure\.py|/render_timelapse\.py|/instagram_auto_publish\.py|/instagram_carousel_publish\.py'
if [[ "$mode" != "force" ]] && pgrep -af "$busy_pattern" >/dev/null; then
  echo "Capture or postprocessing is active; reboot refused."
  pgrep -af "$busy_pattern" || true
  exit 3
fi

if [[ "$mode" == "check" ]]; then
  echo "Reboot helper check passed."
  exit 0
fi

sync
systemd-run --quiet --unit=timelapser-director-reboot --on-active=5s \
  /usr/bin/systemctl reboot
echo "Reboot scheduled in 5 seconds."
