#!/usr/bin/env bash
set -euo pipefail

mode="${1:-}"
case "$mode" in
  check|request|force|shutdown|force-shutdown) ;;
  *) echo "Usage: $0 {check|request|force|shutdown|force-shutdown}" >&2; exit 2 ;;
esac

if systemctl list-jobs --no-legend 2>/dev/null | grep -Eq 'reboot\.target|poweroff\.target|systemd-(reboot|poweroff)'; then
  echo "A reboot or shutdown is already scheduled."
  exit 4
fi

busy_pattern='/timelapser_v5(_aperture_priority)?\.py|/timelapser_postprocess_v5\.py|/download_run_fullres\.py|/smooth_exposure\.py|/render_timelapse\.py|/instagram_auto_publish\.py|/instagram_carousel_publish\.py'
if [[ "$mode" != "force" && "$mode" != "force-shutdown" ]] && pgrep -af "$busy_pattern" >/dev/null; then
  echo "Capture or postprocessing is active; power action refused."
  pgrep -af "$busy_pattern" || true
  exit 3
fi

if [[ "$mode" == "check" ]]; then
  echo "Reboot helper check passed."
  exit 0
fi

sync
if [[ "$mode" == "shutdown" || "$mode" == "force-shutdown" ]]; then
  systemd-run --quiet --unit=timelapser-director-shutdown --on-active=5s \
    /usr/bin/systemctl poweroff
  echo "Shutdown scheduled in 5 seconds."
else
  systemd-run --quiet --unit=timelapser-director-reboot --on-active=5s \
    /usr/bin/systemctl reboot
  echo "Reboot scheduled in 5 seconds."
fi
