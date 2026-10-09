#!/usr/bin/env python3
"""Persistent scheduler adapter for wired Timelapser V6."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import timelapser_scheduler as scheduler
import v6_config


ROOT = Path(__file__).resolve().parent
scheduler.SCHEDULE_FILE = ROOT / "schedule_v6.py"
scheduler.STATE_FILE = ROOT / "scheduler_state_v6.json"
scheduler.LOG_FILE = ROOT / "scheduler_v6.log"
scheduler.PREFLIGHT_ALERT_FILE = v6_config.PREFLIGHT_ALERT_FILE
scheduler.SCHEDULER_PAUSE_FILE = v6_config.SCHEDULER_PAUSE_FILE


def run_preflight(mod, mission) -> tuple[bool, list[str]]:
    checks: list[str] = []
    ok = True
    for label, path in (
        ("python", Path(mod.PYTHON)),
        ("wired capture script", Path(mod.TIMELAPSER_SCRIPT)),
        ("V6 postprocess script", Path(mod.POSTPROCESS_SCRIPT)),
    ):
        if path.exists():
            checks.append(f"OK {label}: {path}")
        else:
            ok = False
            checks.append(f"FAIL {label} missing: {path}")

    mount = Path(mod.OUTPUT_MOUNT)
    output = Path(mod.OUTPUT_ROOT)
    if not mount.is_mount():
        ok = False
        checks.append(f"FAIL external output drive is not mounted: {mount}")
    else:
        output.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(output).free / 1024**3
        checks.append(f"OK output disk free: {free_gb:.1f} GB")

    if v6_config.FOCUS_GPIO is None or v6_config.SHUTTER_GPIO is None:
        ok = False
        checks.append("FAIL wired relay GPIO mapping is not configured")
    elif v6_config.FOCUS_GPIO == v6_config.SHUTTER_GPIO:
        ok = False
        checks.append("FAIL focus and shutter are configured on the same GPIO")
    else:
        checks.append(
            f"OK wired relays configured: AF GPIO{v6_config.FOCUS_GPIO}, "
            f"RLS GPIO{v6_config.SHUTTER_GPIO}, "
            f"{'active-low' if v6_config.RELAY_ACTIVE_LOW else 'active-high'}"
        )

    gpio_check = subprocess.run(
        [mod.PYTHON, "-c", "import gpiozero"], capture_output=True, text=True, check=False
    )
    if gpio_check.returncode == 0:
        checks.append("OK gpiozero available")
    else:
        ok = False
        checks.append("FAIL gpiozero is unavailable in the Timelapser Python environment")

    # The camera must not be enumerated as USB storage while its shutter is in use.
    lsblk = subprocess.run(
        ["lsblk", "-ndo", "MODEL"], capture_output=True, text=True, check=False
    )
    if "E-M5MarkIII" in lsblk.stdout:
        ok = False
        checks.append("FAIL camera is in USB storage mode; release the MEGA4 camera port")
    else:
        checks.append("OK camera is not attached as USB storage")

    checks.append("OK camera Wi-Fi is not required by V6")
    return ok, checks


def build_command(mod, mission, now: datetime) -> list[str]:
    remaining = max(1, int((mission.end_dt - now).total_seconds()))
    max_frames = max(1, int(remaining // mission.interval))
    if mission.max_frames is not None:
        max_frames = min(max_frames, mission.max_frames)
    return [
        mod.PYTHON,
        mod.TIMELAPSER_SCRIPT,
        "--mode", mission.mode,
        "--interval", str(mission.interval),
        "--max-frames", str(max_frames),
        "--max-run-minutes", str(max(1.0, remaining / 60)),
        "--defer-postprocess",
    ]


def run_one_deferred_postprocess(mod) -> bool:
    requests = scheduler.deferred_requests(Path(mod.OUTPUT_ROOT))
    if not requests:
        return False
    _, request_path, request = requests[0]
    run_dir = Path(request.get("run_dir") or request_path.parent)
    command = [mod.PYTHON, mod.POSTPROCESS_SCRIPT, str(run_dir)]
    if not request.get("publish_instagram", False):
        command.append("--skip-instagram-publish")
    request.update(
        status="running",
        processing_started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        command=command,
    )
    scheduler.save_deferred_request(request_path, request)
    scheduler.log(f"V6 DEFERRED PROCESS START {run_dir.name}")
    proc = subprocess.run(command, cwd=str(ROOT))
    request.update(
        status="complete" if proc.returncode == 0 else "failed",
        returncode=proc.returncode,
        processing_ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    scheduler.save_deferred_request(request_path, request)
    scheduler.log(f"V6 DEFERRED PROCESS END {run_dir.name}: returncode={proc.returncode}")
    return True


scheduler.run_preflight = run_preflight
scheduler.build_command = build_command
scheduler.run_one_deferred_postprocess = run_one_deferred_postprocess


if __name__ == "__main__":
    raise SystemExit(scheduler.main())
