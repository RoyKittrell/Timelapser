#!/usr/bin/env python3
"""Persistent scheduler adapter for wired Timelapser V6."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import timelapser_scheduler as scheduler
import v6_config
from v6_camera_alive import ensure_camera_alive
import mega4_usb_import


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

    if ok:
        camera_ok, camera_message = ensure_camera_alive(mission.key)
        checks.append(camera_message)
        if not camera_ok:
            ok = False
    else:
        checks.append("SKIP camera test shot because an earlier preflight check failed")
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
        "--event-time", mission.event_dt.isoformat(),
        "--max-frames", str(max_frames),
        "--max-run-minutes", str(max(1.0, remaining / 60)),
        "--skip-autofocus",
        "--defer-postprocess",
    ]


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def capture_due_for_preemption(mod) -> scheduler.Mission | None:
    if v6_config.SCHEDULER_PAUSE_FILE.exists():
        return None
    now = datetime.now().astimezone()
    state = scheduler.load_state()
    for mission in scheduler.build_missions(mod):
        if not mission.enabled or not (mission.preflight_dt <= now < mission.end_dt):
            continue
        status = state.get("events", {}).get(mission.key, {}).get("status", "pending")
        if status not in {"complete", "failed_terminal", "missed"}:
            return mission
    return None


def cleanup_interrupted_stage(run_dir: Path, stage: str) -> list[str]:
    removed: list[str] = []

    def remove_path(path: Path) -> None:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
        removed.append(str(path))

    if stage == "import_wired_v6_jpegs_via_mega4_usb":
        device = mega4_usb_import.camera_partition()
        mega4_usb_import.cleanup(device)
    elif stage == "smooth_exposure_v2":
        remove_path(run_dir / "frames_smoothed_luma_v2")
    elif stage == "render_videos":
        for path in (
            list(run_dir.glob(f"{run_dir.name}_final_*.mp4"))
            + list(run_dir.glob("_overlay_stage_*"))
            + [run_dir / "_render_stage", run_dir / "render_summary_v3_final.json"]
        ):
            remove_path(path)
    elif stage == "create_instagram_carousel_formats":
        for path in list(run_dir.glob(f"{run_dir.name}_final_instagram-carousel_*.mp4")) + [
            run_dir / "instagram_formats_summary.json"
        ]:
            remove_path(path)
    return removed


def pause_postprocess_for_capture(
    proc: subprocess.Popen,
    run_dir: Path,
    request_path: Path,
    request: dict,
    mission: scheduler.Mission,
) -> None:
    live_path = run_dir / "postprocess_live_status.json"
    live = read_json(live_path)
    interrupted_step = str(live.get("current_step") or "preparing")
    scheduler.log(
        f"V6 DEFERRED PROCESS PAUSE {run_dir.name}: {interrupted_step} interrupted for {mission.key}"
    )
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    except ProcessLookupError:
        pass

    removed = cleanup_interrupted_stage(run_dir, interrupted_step)
    paused_at = datetime.now().astimezone().isoformat(timespec="seconds")
    request.update(
        status="pending",
        paused_at=paused_at,
        paused_for_mission=mission.key,
        interrupted_step=interrupted_step,
        interrupted_outputs_removed=removed,
        preemption_count=int(request.get("preemption_count", 0)) + 1,
    )
    scheduler.save_deferred_request(request_path, request)
    live.update(
        status="paused_for_capture",
        current_step="",
        paused_step=interrupted_step,
        paused_at=paused_at,
        paused_for_mission=mission.key,
        error="",
    )
    write_json(live_path, live)


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
    proc = subprocess.Popen(command, cwd=str(ROOT), start_new_session=True)
    while proc.poll() is None:
        mission = capture_due_for_preemption(mod)
        if mission is not None:
            pause_postprocess_for_capture(proc, run_dir, request_path, request, mission)
            return True
        time.sleep(5)
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
