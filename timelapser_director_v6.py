#!/usr/bin/env python3
"""Small operational dashboard for wired Timelapser V6."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from PIL import Image

import v6_config
import mega4_usb_import
import timelapser_scheduler as scheduler_core
from enclosure_sensor import read_sht3x
from v6_camera_alive import ensure_camera_alive, purge_stale_test_shot
from v6_intervalometer import WiredCamera


CAMERA_USB_HELPER = Path("/usr/local/sbin/timelapser-camera-usb")
REBOOT_HELPER = Path("/usr/local/sbin/timelapser-reboot")
ENV_FILE = Path("/home/roy/Timelapser Sept2026/env/.env")
MANUAL_CAMERA_LOCK = v6_config.CAMERA_OPERATION_LOCK
MANUAL_SHOT_SETTLE_SECONDS = 10.0
CAMERA_USB_SETTLE_SECONDS = 20.0
WAKE_PULSE_SECONDS = getattr(v6_config, "WAKE_PULSE_SECONDS", 0.5)
WAKE_SETTLE_SECONDS = getattr(v6_config, "WAKE_SETTLE_SECONDS", 2.0)
FOCUS_PULSE_SECONDS = getattr(v6_config, "FOCUS_PULSE_SECONDS", 2.0)
FOCUS_SETTLE_SECONDS = getattr(v6_config, "FOCUS_SETTLE_SECONDS", 0.75)
TEST_SHOT_DIR = v6_config.TEST_SHOT_DIR
TEST_SHOT_IMAGE = v6_config.TEST_SHOT_IMAGE
TEST_SHOT_STATE = v6_config.TEST_SHOT_STATE
ENCLOSURE_CLIMATE_CACHE = v6_config.CONTROL_DIR / "enclosure_climate.json"
ACTIVE_CAMERA_JOB_PATTERNS = (
    "timelapser_v6.py",
    "timelapser_postprocess_v6.py",
    "mega4_usb_import.py",
    "render_timelapse.py",
    "instagram_auto_publish.py",
)
MANUAL_CAPTURE_LOG = v6_config.CONTROL_DIR / "manual_capture.log"
PENDING_MANUAL_CAPTURE = v6_config.CONTROL_DIR / "pending_manual_capture.json"
MAX_MANUAL_CAPTURE_MINUTES = 12 * 60
MANUAL_OUTPUT_FPS = 30.0
MANUAL_CONFIRMATION_MINUTES = 10

load_dotenv(ENV_FILE)


st.set_page_config(page_title="Timelapser V6", page_icon="camera", layout="wide")
st.markdown(
    """
    <style>
    .block-container {max-width: 980px; padding: 3.75rem 0.8rem 3rem;}
    h1 {line-height: 1.15 !important; margin-top: 0 !important;}
    [data-testid="stMetricValue"] {font-size: 1.35rem;}
    @media (max-width: 600px) {
      .block-container {padding: 3.5rem 0.55rem 2rem;}
      h1 {font-size: 1.75rem !important;}
      [data-testid="stHorizontalBlock"] {gap: 0.35rem; flex-wrap: wrap;}
      [data-testid="stHorizontalBlock"] > [data-testid="column"] {
        flex: 1 1 calc(50% - 0.35rem) !important;
        min-width: calc(50% - 0.35rem) !important;
      }
      [data-testid="stMetricValue"] {font-size: 1.15rem;}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def next_scheduled_mission():
    # schedule_v6 imports schedule.py with ``from schedule import *``. In this
    # long-running Streamlit process that underlying module can remain cached
    # after the calendar is regenerated, so read the mission source directly.
    schedule_path = Path(__file__).resolve().parent / "schedule.py"
    spec = importlib.util.spec_from_file_location("dashboard_schedule", schedule_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = datetime.now().astimezone()
    future = [
        mission
        for mission in scheduler_core.build_missions(module)
        if mission.enabled and mission.end_dt > now
    ]
    return min(future, key=lambda mission: mission.start_dt) if future else None


def human_countdown(target: datetime) -> str:
    seconds = max(0, int((target - datetime.now().astimezone()).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def clear_test_shot_cache() -> None:
    TEST_SHOT_IMAGE.unlink(missing_ok=True)
    TEST_SHOT_STATE.unlink(missing_ok=True)


def enclosure_climate() -> tuple[dict, str | None]:
    try:
        reading = {
            **read_sht3x().as_dict(),
            "measured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        ENCLOSURE_CLIMATE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache_tmp = ENCLOSURE_CLIMATE_CACHE.with_suffix(".tmp")
        cache_tmp.write_text(json.dumps(reading, indent=2) + "\n", encoding="utf-8")
        cache_tmp.replace(ENCLOSURE_CLIMATE_CACHE)
        return reading, None
    except Exception as exc:
        cached = read_json(ENCLOSURE_CLIMATE_CACHE)
        return cached, f"{type(exc).__name__}: {exc}"


def cpu_temperature_c() -> float | None:
    try:
        return float(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def render_enclosure_climate() -> None:
    climate, climate_error = enclosure_climate()
    cpu_temp = cpu_temperature_c()
    if climate:
        climate_cols = st.columns(5)
        climate_cols[0].metric("Temperature", f"{climate['temperature_c']:.1f} °C")
        climate_cols[1].metric("Humidity", f"{climate['humidity_percent']:.1f}%")
        climate_cols[2].metric("Dew point", f"{climate['dew_point_c']:.1f} °C")
        climate_cols[3].metric("Dew margin", f"{climate['dew_margin_c']:.1f} °C")
        climate_cols[4].metric("Pi CPU", f"{cpu_temp:.1f} °C" if cpu_temp is not None else "Unavailable")
        if climate["dew_margin_c"] <= 1.0:
            st.error("Condensation risk: enclosure temperature is within 1 °C of the dew point.")
        elif climate["dew_margin_c"] <= 3.0:
            st.warning("Low dew margin: monitor the enclosure for condensation.")
        measured_at = pd.to_datetime(climate.get("measured_at"), errors="coerce")
        if not pd.isna(measured_at):
            st.caption(f"Last enclosure reading: {measured_at.strftime('%d %b, %H:%M:%S')}")
        return
    st.metric("Pi CPU temperature", f"{cpu_temp:.1f} °C" if cpu_temp is not None else "Unavailable")
    st.caption("Waiting for the first enclosure reading.")


def format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"


def format_total_time_left(seconds: float) -> str:
    total_minutes = max(0, math.ceil(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"Estimated {hours}hrs {minutes}min left"
    return f"{secs}s"


POSTPROCESS_STAGES = (
    ("import_wired_v6_jpegs_via_mega4_usb", "Download full JPEGs"),
    ("validate_and_repair_jpegs", "Validate JPEGs"),
    ("smooth_exposure_v2", "Smooth exposure"),
    ("validate_smoothed_jpegs", "Validate smoothed frames"),
    ("render_videos", "Create videos and overlays"),
    ("create_instagram_carousel_formats", "Create Instagram formats"),
    ("queue_instagram_reel", "Prepare Instagram package"),
    ("publish_instagram_carousel_and_reel", "Upload to Instagram"),
    *(((("copy_videos_to_mac", "Copy videos to Mac"),) if v6_config.COPY_VIDEOS_TO_MAC else ())),
    ("prune_rendered_images_older_than_30_days", "Apply image retention"),
)

FRAME_SCALED_STAGES = {
    "import_wired_v6_jpegs_via_mega4_usb",
    "import_fullres_jpegs_via_mega4_usb",
    "validate_and_repair_jpegs",
    "validate_smoothed_jpegs",
    "smooth_exposure_v2",
    "render_videos",
    "create_instagram_carousel_formats",
}


def completed_workflow(summary: dict) -> bool:
    # Copy-to-Mac is a convenience step after the media workflow has succeeded.
    return str(summary.get("status")) in {"complete", "failed_copy_to_mac"}


def postprocess_step_averages(run_dir: Path) -> dict[str, float]:
    samples: dict[str, list[float]] = {}
    target_manifest = read_json(run_dir / "run_manifest.json")
    target_frames = int(target_manifest.get("completed_frames") or target_manifest.get("max_frames") or 0)
    try:
        summaries = sorted(
            run_dir.parent.glob("*/postprocess_workflow_summary.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:20]
    except OSError:
        summaries = []
    for path in summaries:
        summary = read_json(path)
        if not completed_workflow(summary):
            continue
        source_manifest = read_json(path.parent / "run_manifest.json")
        source_frames = int(source_manifest.get("completed_frames") or source_manifest.get("max_frames") or 0)
        for step in summary.get("steps") or []:
            try:
                if int(step.get("returncode", 1)) != 0:
                    continue
                name = str(step["name"])
                elapsed = float(step["elapsed_seconds"])
                if name in FRAME_SCALED_STAGES and target_frames and source_frames:
                    elapsed *= target_frames / source_frames
                samples.setdefault(name, []).append(elapsed)
            except (KeyError, TypeError, ValueError):
                continue
    return {name: sum(values) / len(values) for name, values in samples.items() if values}


def successful_capture_seconds_per_frame(run_dir: Path, mode: str) -> float | None:
    samples = []
    try:
        candidates = sorted(
            run_dir.parent.glob("*/run_manifest.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:30]
    except OSError:
        candidates = []
    for path in candidates:
        if path.parent == run_dir:
            continue
        manifest = read_json(path)
        summary = read_json(path.parent / "postprocess_workflow_summary.json")
        if str(manifest.get("mode")) != mode or manifest.get("error") or not completed_workflow(summary):
            continue
        started = pd.to_datetime(manifest.get("started_at"), errors="coerce")
        ended = pd.to_datetime(manifest.get("ended_at"), errors="coerce")
        frames = int(manifest.get("completed_frames") or 0)
        if pd.isna(started) or pd.isna(ended) or frames <= 0:
            continue
        samples.append(max(0.0, (ended.to_pydatetime() - started.to_pydatetime()).total_seconds()) / frames)
    return sum(samples) / len(samples) if samples else None


def count_jpegs(folder: Path) -> int:
    try:
        return sum(1 for path in folder.iterdir() if path.suffix.lower() in {".jpg", ".jpeg"})
    except OSError:
        return 0


def stage_frame_progress(run_dir: Path, current: str, total: int) -> tuple[int, int]:
    if not total:
        return 0, 0
    if current == "import_wired_v6_jpegs_via_mega4_usb":
        return min(total, count_jpegs(run_dir / "frames_full_jpeg")), total
    if current == "smooth_exposure_v2":
        return min(total, count_jpegs(run_dir / "frames_smoothed_luma_v2")), total
    if current in {"validate_and_repair_jpegs", "validate_smoothed_jpegs"}:
        try:
            log_tail = (run_dir / "postprocess_workflow.log").read_text(
                encoding="utf-8", errors="replace"
            )[-40000:]
        except OSError:
            return 0, total
        matches = re.findall(r"JPEG validation progress:\s*(\d+)/(\d+)", log_tail)
        if matches:
            done, expected = map(int, matches[-1])
            return done, expected
    if current == "render_videos":
        done = len(list(run_dir.glob(f"{run_dir.name}_final_instagram-reel_*.mp4")))
        return min(3, done), 3
    if current == "create_instagram_carousel_formats":
        done = len(list(run_dir.glob(f"{run_dir.name}_final_instagram-carousel_*.mp4")))
        return min(3, done), 3
    return 0, 0


def completed_postprocess_steps(
    run_dir: Path,
    manifest: dict,
    post: dict,
    live: dict,
) -> set[str]:
    completed = set(str(name) for name in (live.get("completed_steps") or []))
    completed.update(
        str(step.get("name")) for step in (post.get("steps") or [])
        if step.get("returncode") == 0
    )
    total_frames = int(manifest.get("completed_frames") or manifest.get("max_frames") or 0)
    downloaded = count_jpegs(run_dir / "frames_full_jpeg")
    import_summary = read_json(run_dir / "v6_usb_import_summary.json")
    if import_summary.get("status") == "complete" and int(import_summary.get("imported_frames") or 0) == downloaded:
        completed.add("import_wired_v6_jpegs_via_mega4_usb")
    if total_frames and downloaded >= total_frames:
        completed.add("import_wired_v6_jpegs_via_mega4_usb")
        completed.add("import_fullres_jpegs_via_mega4_usb")
    return completed


def render_postprocess_progress(run_dir: Path, manifest: dict, post: dict, live: dict) -> None:
    if not live and not post:
        st.info("Post-processing has not started.")
        return

    status = str(live.get("status") or post.get("status") or "waiting")
    current = str(live.get("current_step") or "")
    completed = completed_postprocess_steps(run_dir, manifest, post, live)
    publish_enabled = bool(live.get("publish_instagram", post.get("publish_instagram", False)))
    stages = [
        item for item in POSTPROCESS_STAGES
        if publish_enabled or item[0] != "publish_instagram_carousel_and_reel"
    ]
    labels = dict(POSTPROCESS_STAGES)
    total_frames = int(manifest.get("completed_frames") or manifest.get("max_frames") or 0)
    stage_done, stage_total = stage_frame_progress(run_dir, current, total_frames)
    stage_fraction = stage_done / stage_total if stage_total else 0.0
    done_count = sum(1 for name, _ in stages if name in completed)
    overall_fraction = min(1.0, (done_count + stage_fraction) / max(1, len(stages)))

    now = datetime.now().astimezone()
    started = pd.to_datetime(live.get("started_at"), errors="coerce")
    elapsed = None if pd.isna(started) else max(0.0, (now - started.to_pydatetime()).total_seconds())
    if elapsed is None and post.get("elapsed_seconds") is not None:
        elapsed = float(post["elapsed_seconds"])

    eta = None
    finish_at = None
    if status == "running":
        averages = postprocess_step_averages(run_dir)
        remaining = 0.0
        known = 0
        current_started = pd.to_datetime(live.get("current_step_started_at"), errors="coerce")
        current_elapsed = 0.0 if pd.isna(current_started) else max(
            0.0, (now - current_started.to_pydatetime()).total_seconds()
        )
        for name, _ in stages:
            if name in completed:
                continue
            average = averages.get(name)
            if average is not None:
                if name == current and stage_total and stage_done:
                    average = max(current_elapsed, current_elapsed / stage_fraction)
                remaining += max(0.0, average - current_elapsed) if name == current else average
                known += 1
        if known:
            eta = remaining
            finish_at = now + timedelta(seconds=remaining)

    cols = st.columns(5)
    cols[0].metric("Status", status.replace("_", " ").title())
    cols[1].metric("Current stage", labels.get(current, current.replace("_", " ").title()) or "Finished")
    cols[2].metric("Stage items", f"{stage_done}/{stage_total}" if stage_total else "—")
    cols[3].metric("Elapsed", format_duration(elapsed))
    cols[4].metric("ETA", format_duration(eta) if status == "running" else ("Complete" if status == "complete" else "—"))
    st.progress(overall_fraction)
    if stage_total and current:
        st.caption(f"Current stage: {stage_done}/{stage_total}")
        st.progress(min(1.0, stage_fraction))
    if finish_at is not None:
        st.caption(f"Estimated workflow completion: {finish_at.strftime('%H:%M %Z')}")
    stage_text = []
    for name, label in stages:
        state = "Done" if name in completed else "Now" if name == current else "Waiting"
        stage_text.append(f"{state}: {label}")
    st.caption(" | ".join(stage_text))
    if status == "failed_copy_to_mac":
        st.warning("Videos and Instagram upload completed; the optional copy to Mac was unavailable.")
    elif status.startswith("failed"):
        failed_stage = labels.get(current, current) or "an unknown stage"
        st.error(str(live.get("error") or f"Workflow stopped during {failed_stage}."))


def render_capture_progress(run_dir: Path) -> None:
    manifest = live_manifest(run_dir)
    relay_fires = int(manifest.get("completed_frames") or count_rows(run_dir / "trigger_timeline.csv"))
    planned = int(manifest.get("max_frames") or 0)
    capture_complete = manifest.get("status") in {"capture_complete", "complete", "postprocessing"}
    import_summary = read_json(run_dir / "v6_usb_import_summary.json")
    imported_jpegs = int(import_summary.get("imported_frames") or count_jpegs(run_dir / "frames_full_jpeg"))
    import_complete = import_summary.get("status") == "complete"
    unmatched = int(import_summary.get("unmatched_triggers") or 0) if import_complete else None

    cols = st.columns(4)
    cols[0].metric("State", manifest.get("status", "unknown").replace("_", " ").title())
    cols[1].metric("Mode", str(manifest.get("mode", "unknown")).title())
    interval_label = f"{float(manifest.get('interval_seconds', 0)):g}s"
    if manifest.get("adaptive_cadence"):
        interval_label = f"Adaptive ({interval_label} min)"
    cols[2].metric("Interval", interval_label)

    capture_eta = capture_eta_seconds(manifest, run_dir)
    capture_finish = None
    if capture_eta is not None:
        capture_finish = datetime.now().astimezone() + timedelta(seconds=capture_eta)
    eta_label = format_duration(capture_eta) if capture_eta is not None else (
        "Complete" if manifest.get("status") == "capture_complete" else "—"
    )
    cols[3].metric("Capture ETA", eta_label)

    counts = st.columns(3)
    relay_label = f"{relay_fires}/{planned}" if planned and not capture_complete else str(relay_fires)
    counts[0].metric("Relay fires", relay_label)
    counts[1].metric("Actual JPEGs", str(imported_jpegs) if import_complete else "Pending import")
    counts[2].metric("Unmatched fires", str(unmatched) if unmatched is not None else "Pending import")
    if planned or capture_complete:
        st.progress(1.0 if capture_complete else min(1.0, relay_fires / planned))
    if capture_finish is not None:
        st.caption(f"Estimated capture completion: {capture_finish.strftime('%H:%M %Z')}")
    if manifest.get("error"):
        st.error(str(manifest["error"]))


WORKFLOW_GROUPS = (
    ("Capture", ()),
    ("Download", ("import_wired_v6_jpegs_via_mega4_usb",)),
    ("Validate", ("validate_and_repair_jpegs", "validate_smoothed_jpegs")),
    ("Smooth", ("smooth_exposure_v2",)),
    ("Create videos", ("render_videos", "create_instagram_carousel_formats")),
    ("Prepare and upload", ("queue_instagram_reel", "publish_instagram_carousel_and_reel")),
    *(((("Copy to Mac", ("copy_videos_to_mac",)),) if v6_config.COPY_VIDEOS_TO_MAC else ())),
)


def capture_eta_seconds(manifest: dict, run_dir: Path | None = None) -> float | None:
    completed = int(manifest.get("completed_frames") or 0)
    planned = int(manifest.get("max_frames") or 0)
    if manifest.get("status") not in {"starting", "capturing"} or not planned or completed >= planned:
        return None
    started_at = pd.to_datetime(manifest.get("started_at"), errors="coerce")
    estimate = None
    historical_rate = None
    if run_dir is not None:
        historical_rate = successful_capture_seconds_per_frame(run_dir, str(manifest.get("mode") or ""))
    if historical_rate is not None:
        estimate = historical_rate * (planned - completed)
    elif completed > 0 and not pd.isna(started_at):
        elapsed = max(0.0, (datetime.now().astimezone() - started_at.to_pydatetime()).total_seconds())
        estimate = elapsed / completed * (planned - completed)
    elif not manifest.get("adaptive_cadence"):
        estimate = (planned - completed) * float(manifest.get("interval_seconds") or 0)
    try:
        hard_stop = started_at.to_pydatetime() + timedelta(minutes=float(manifest["max_run_minutes"]))
        hard_stop_eta = max(0.0, (hard_stop - datetime.now().astimezone()).total_seconds())
        estimate = hard_stop_eta if estimate is None else min(estimate, hard_stop_eta)
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return estimate


def render_workflow_overview(run_dir: Path, manifest: dict) -> None:
    post = read_json(run_dir / "postprocess_workflow_summary.json")
    live = read_json(run_dir / "postprocess_live_status.json")
    completed_steps = completed_postprocess_steps(run_dir, manifest, post, live)
    current_step = str(live.get("current_step") or "")
    capture_status = str(manifest.get("status") or "unknown")
    capture_done = capture_status in {"capture_complete", "complete", "postprocessing"} or bool(post or live)
    capture_running = capture_status in {"starting", "capturing"}
    planned = int(manifest.get("max_frames") or 0)
    captured = int(manifest.get("completed_frames") or count_rows(run_dir / "trigger_timeline.csv"))
    capture_fraction = min(1.0, captured / planned) if planned else 0.0
    averages = postprocess_step_averages(run_dir)

    group_states = []
    active_group = 0
    for index, (label, steps) in enumerate(WORKFLOW_GROUPS):
        if not steps:
            done = capture_done
            running = capture_running
            estimate = capture_eta_seconds(manifest, run_dir)
        else:
            done = all(step in completed_steps for step in steps)
            running = current_step in steps and str(live.get("status")) == "running"
            estimate = sum(averages.get(step, 0.0) for step in steps) or None
            if running and estimate is not None:
                current_started = pd.to_datetime(live.get("current_step_started_at"), errors="coerce")
                current_elapsed = 0.0 if pd.isna(current_started) else max(
                    0.0,
                    (datetime.now().astimezone() - current_started.to_pydatetime()).total_seconds(),
                )
                estimate = max(0.0, estimate - current_elapsed)
        if running:
            active_group = index
            state = "Running"
        elif done:
            active_group = max(active_group, index + 1)
            state = "Complete"
        else:
            state = "Waiting"
        group_states.append((label, state, estimate))

    if capture_running:
        overall = capture_fraction / len(WORKFLOW_GROUPS)
    elif active_group >= len(WORKFLOW_GROUPS):
        overall = 1.0
    else:
        overall = active_group / len(WORKFLOW_GROUPS)

    remaining_estimates = [
        estimate
        for _, state, estimate in group_states
        if state in {"Running", "Waiting"} and estimate is not None
    ]
    if remaining_estimates:
        st.markdown(f"**{format_total_time_left(sum(remaining_estimates))}**")
    st.progress(min(1.0, overall))
    for label, state, estimate in group_states:
        timing = ""
        if state == "Running" and estimate is not None:
            timing = f" · ETA {format_duration(estimate)}"
        elif state == "Waiting" and estimate is not None:
            timing = f" · est. {format_duration(estimate)}"
        st.markdown(f"**{label}** · {state}{timing}")


def render_primary_status() -> None:
    streak, total_published = publication_counts()
    reliability_cols = st.columns(2)
    reliability_cols[0].metric(
        "Autonomous IG streak",
        f"{streak} timelapse{'s' if streak != 1 else ''}",
    )
    reliability_cols[1].metric(
        "Published timelapses",
        str(total_published),
    )
    st.caption("Consecutive scheduled sunrise/sunset runs successfully published to Instagram.")

    active = read_json(v6_config.ACTIVE_RUN)
    active_dir = Path(str(active.get("run_dir") or ""))
    if active.get("status") in {"starting", "capturing"} and active_dir.is_dir():
        manifest = live_manifest(active_dir)
        st.subheader("Timelapse running")
        cols = st.columns(4)
        cols[0].metric("Mission", str(manifest.get("mode") or "general").title())
        completed = int(manifest.get("completed_frames") or count_rows(active_dir / "trigger_timeline.csv"))
        planned = int(manifest.get("max_frames") or 0)
        cols[1].metric("Frames", f"{completed}/{planned}" if planned else str(completed))
        cols[2].metric("Capture ETA", format_duration(capture_eta_seconds(manifest, active_dir)))
        cols[3].metric("Interval", f"{float(manifest.get('interval_seconds') or 0):g}s")
        render_workflow_overview(active_dir, manifest)
        return

    latest_runs = runs()
    latest = latest_runs[0] if latest_runs else None
    if latest is not None:
        live = read_json(latest / "postprocess_live_status.json")
        if live.get("status") in {"running", "paused_for_capture"}:
            manifest = live_manifest(latest)
            if live.get("status") == "paused_for_capture":
                st.subheader("Processing paused for scheduled timelapse")
                mission = str(live.get("paused_for_mission") or "the next capture")
                stage = str(live.get("paused_step") or "processing").replace("_", " ")
                st.info(f"Paused during {stage}; it will resume after {mission}.")
            else:
                st.subheader("Processing timelapse")
            st.metric("Mission", str(manifest.get("mode") or "general").title())
            render_workflow_overview(latest, manifest)
            return

    mission = next_scheduled_mission()
    st.subheader("Next timelapse")
    if mission:
        cols = st.columns(5)
        cols[0].metric("Mission", mission.mode.title())
        cols[1].metric("Date", mission.start_dt.strftime("%d %b"))
        cols[2].metric("Starts", mission.start_dt.strftime("%H:%M"))
        cols[3].metric("Ends", mission.end_dt.strftime("%H:%M"))
        cols[4].metric("Starts in", human_countdown(mission.start_dt))
        st.caption(
            f"Preflight {mission.preflight_dt.strftime('%d %b, %H:%M')} · "
            f"Event {mission.event_dt.strftime('%H:%M')} · Interval {mission.interval:g}s"
        )
    else:
        st.warning("No future enabled timelapse is present in the schedule.")


def render_postprocess_panel(run_dir: Path) -> None:
    manifest = live_manifest(run_dir)
    post = read_json(run_dir / "postprocess_workflow_summary.json")
    live = read_json(run_dir / "postprocess_live_status.json")
    render_postprocess_progress(run_dir, manifest, post, live)


def queue_photos_import(run_dir: Path, video: Path) -> None:
    request = run_dir / "photos_import_request.json"
    payload = {
        "status": "requested",
        "run_id": run_dir.name,
        "video_name": video.name,
        "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "message": "Waiting for the Mac import agent",
    }
    temporary = request.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(request)


def output_root() -> Path:
    return v6_config.DEFAULT_OUTPUT_ROOT


def runs() -> list[Path]:
    root = output_root()
    if not root.is_dir():
        return []
    return sorted(
        (path for path in root.glob("*_v6_wired*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def publication_counts() -> tuple[int, int]:
    streak = 0
    total_published = 0
    streak_intact = True
    # Run names begin with YYYYMMDD_HHMMSS and are the capture chronology;
    # modification times change later during retries and maintenance.
    for run_dir in sorted(runs(), key=lambda path: path.name, reverse=True):
        manifest = read_json(run_dir / "run_manifest.json")
        capture_status = str(manifest.get("status") or "")
        post_summary = read_json(run_dir / "postprocess_workflow_summary.json")
        live_summary = read_json(run_dir / "postprocess_live_status.json")
        publish_receipt = read_json(run_dir / "instagram_publish_receipt.json")
        post_status = str(post_summary.get("status") or "")

        # General and manual tests are outside the autonomous publishing streak.
        if str(manifest.get("mode") or "") not in {"sunrise", "sunset"}:
            continue

        # A run still capturing or processing has not earned or broken the streak.
        if capture_status in {"starting", "capturing"}:
            continue
        if live_summary.get("status") == "running":
            continue

        finalized = (
            capture_status.startswith(("failed", "aborted", "stopped"))
            or (post_status and post_status not in {"running", "waiting"})
            or bool(publish_receipt)
        )
        if not finalized:
            continue

        published = publish_receipt.get("status") == "complete"
        if published:
            total_published += 1
            if streak_intact:
                streak += 1
        else:
            streak_intact = False
    return streak, total_published


def count_rows(path: Path) -> int:
    try:
        return max(0, sum(1 for _ in path.open(encoding="utf-8")) - 1)
    except OSError:
        return 0


def live_manifest(run_dir: Path) -> dict:
    manifest = read_json(run_dir / "run_manifest.json")
    active = read_json(v6_config.ACTIVE_RUN)
    try:
        same_run = Path(str(active.get("run_dir", ""))).resolve() == run_dir.resolve()
    except (OSError, ValueError):
        same_run = False
    if same_run and active.get("status") in {"starting", "capturing"}:
        return {**manifest, **active}
    return manifest


def active_camera_jobs() -> list[str]:
    result = subprocess.run(
        ["pgrep", "-af", "|".join(ACTIVE_CAMERA_JOB_PATTERNS)],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def release_camera_usb() -> tuple[bool, str]:
    active = active_camera_jobs()
    if active:
        return False, "Camera USB release refused while capture or post-processing is active."

    try:
        result = subprocess.run(
            ["sudo", "-n", str(CAMERA_USB_HELPER), "discover-and-off"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "Camera USB release timed out; no unverified hub port was changed."
    except OSError as exc:
        return False, f"Camera USB helper could not be started: {exc}"

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return False, f"Camera USB helper refused the operation: {output or 'unknown error'}"
    return True, "Olympus USB released. The hard drive was not switched."


def camera_is_usb_storage() -> bool:
    result = subprocess.run(
        ["lsblk", "-ndo", "MODEL"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "E-M5MarkIII" in result.stdout


def perform_relay_action(action: str) -> tuple[bool, str]:
    try:
        with WiredCamera(
            v6_config.FOCUS_GPIO,
            v6_config.SHUTTER_GPIO,
            active_low=v6_config.RELAY_ACTIVE_LOW,
        ) as camera:
            if action == "autofocus":
                camera.autofocus_once(v6_config.FOCUS_PULSE_SECONDS)
                return True, "Autofocus fired and the AF relay was released."
            if action == "shutter":
                camera.take_picture(v6_config.SHUTTER_PULSE_SECONDS)
                time.sleep(MANUAL_SHOT_SETTLE_SECONDS)
                camera.clear_half_press(v6_config.HALF_PRESS_CLEAR_SECONDS)
                return True, "One photograph triggered; the half-press cleanup pulse completed."
            return False, f"Unsupported manual camera action: {action}"
    except Exception as exc:
        return False, f"Manual camera operation failed: {type(exc).__name__}: {exc}"


def manual_camera_action(action: str) -> tuple[bool, str]:
    active = active_camera_jobs()
    if active:
        return False, "Manual camera control refused while capture or post-processing is active."
    if camera_is_usb_storage():
        return False, "Manual camera control refused while the Olympus is in USB storage mode."

    v6_config.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_CAMERA_LOCK.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, "Another manual camera operation is already running."
        return perform_relay_action(action)


def save_test_shot_state(payload: dict) -> None:
    TEST_SHOT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = TEST_SHOT_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(TEST_SHOT_STATE)


def save_test_shot_preview(source: Path) -> None:
    """Cache a counterclockwise portrait preview without altering camera media."""
    TEST_SHOT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = TEST_SHOT_IMAGE.with_suffix(".tmp.jpg")
    with Image.open(source) as image:
        # The mounted camera stores landscape sensor pixels with an orientation
        # tag that is inconsistent across viewers. Rotate the raw pixels into
        # the dashboard's known-upright portrait orientation.
        rotated = image.rotate(90, expand=True)
        rotated.save(temporary, format="JPEG", quality=92, optimize=True)
    temporary.replace(TEST_SHOT_IMAGE)


def perform_test_shot_sequence() -> tuple[bool, str]:
    """Fire only the shutter relay before handing the camera to USB."""
    try:
        with WiredCamera(
            v6_config.FOCUS_GPIO,
            v6_config.SHUTTER_GPIO,
            active_low=v6_config.RELAY_ACTIVE_LOW,
        ) as camera:
            camera.take_picture(v6_config.SHUTTER_PULSE_SECONDS)
            time.sleep(MANUAL_SHOT_SETTLE_SECONDS)
            camera.clear_half_press(v6_config.HALF_PRESS_CLEAR_SECONDS)
        # The proven post-run path naturally leaves about 18 seconds between
        # releasing the remote contacts and presenting USB. The E-M5 III can
        # remain at "One moment" if storage is attached immediately.
        time.sleep(CAMERA_USB_SETTLE_SECONDS)
        return True, "Shutter fired once; the brief post-shot AF cleanup pulse completed."
    except Exception as exc:
        return False, f"Test-shot relay sequence failed: {type(exc).__name__}: {exc}"


def capture_and_download_test_shot() -> tuple[bool, str]:
    active = active_camera_jobs()
    if active:
        return False, "Test shot refused while capture or post-processing is active."
    if camera_is_usb_storage():
        return False, "Test shot refused while the Olympus is already in USB storage mode."

    requested_at = datetime.now().astimezone().isoformat(timespec="seconds")
    v6_config.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_CAMERA_LOCK.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, "Another manual camera operation is already running."

        ok, message = perform_test_shot_sequence()
        if not ok:
            save_test_shot_state({"status": "failed", "requested_at": requested_at, "error": message})
            return False, message

        device = None
        port_enabled = False
        try:
            mega4_usb_import.set_port("on")
            port_enabled = True
            device = mega4_usb_import.wait_for_device(45)
            sd_root = mega4_usb_import.ensure_mounted(device)
            candidates = [
                path for path in sd_root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
            ]
            if not candidates:
                raise RuntimeError("No JPEG files were found on the Olympus SD card")
            newest = max(candidates, key=lambda path: (path.parent.name.upper(), path.name.upper()))
            save_test_shot_preview(newest)
            completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
            save_test_shot_state({
                "status": "complete",
                "requested_at": requested_at,
                "completed_at": completed_at,
                "source_name": newest.name,
                "image": str(TEST_SHOT_IMAGE),
            })
            return True, f"Test shot downloaded from {newest.name}; camera USB released."
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            save_test_shot_state({"status": "failed", "requested_at": requested_at, "error": error})
            return False, f"The photograph fired, but JPEG download failed: {error}"
        finally:
            if port_enabled:
                mega4_usb_import.cleanup(device)


def set_scheduler_paused(paused: bool, reason: str) -> str:
    if paused:
        payload = {
            "paused": True,
            "requested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": reason or "Director chat request",
            "source": "timelapser_director_v6",
        }
        v6_config.SCHEDULER_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = v6_config.SCHEDULER_PAUSE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temp.replace(v6_config.SCHEDULER_PAUSE_FILE)
        return "Scheduled V6 captures are paused. The pause persists across reboots."

    v6_config.SCHEDULER_PAUSE_FILE.unlink(missing_ok=True)
    return "Scheduled V6 captures are enabled again."


def parse_manual_capture_request(text: str) -> tuple[float, float, bool] | None:
    """Return duration, cadence, and explicit Reel publishing intent."""
    normalized = text.lower().strip()
    timelapse_mentioned = bool(re.search(r"\b(time[ -]?lapse|timelapse)\b", normalized))
    action_requested = bool(re.search(
        r"\b(start|run|begin|launch|do|make|create|take|capture|set up)\b",
        normalized,
    ))
    action_negated = bool(re.search(
        r"\b(?:do not|don't|dont|never)\s+"
        r"(?:start|run|begin|launch|do|make|create|take|capture|set up)\b",
        normalized,
    ))
    if not timelapse_mentioned or not action_requested or action_negated:
        return None

    duration_match = re.search(
        r"\b(?:for\s+)?(\d+(?:\.\d+)?)[\s-]*(hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
        normalized,
    )
    interval_match = re.search(
        r"\b(?:at|every|cadence(?:\s+of)?|interval(?:\s+of)?)\s*"
        r"(\d+(?:\.\d+)?)[\s-]*(seconds?|secs?|sec|s)\b",
        normalized,
    )
    if interval_match is None:
        interval_match = re.search(
            r"\b(\d+(?:\.\d+)?)[\s-]*(seconds?|secs?|sec|s)\s*(?:cadence|interval)\b",
            normalized,
        )
    if duration_match is None or interval_match is None:
        return None

    duration = float(duration_match.group(1))
    duration_unit = duration_match.group(2)
    duration_minutes = duration * 60 if duration_unit.startswith("h") else duration
    interval_seconds = float(interval_match.group(1))
    instagram_mentioned = bool(re.search(r"\b(ig|instagram)\b", normalized))
    publish_verb = bool(re.search(r"\b(post|publish|upload|share)\b", normalized))
    publish_denied = bool(re.search(
        r"\b(?:do not|don't|dont|no|without)\b.{0,24}\b(?:ig|instagram|publish|post|upload)\b",
        normalized,
    ))
    publish_reel = instagram_mentioned and publish_verb and not publish_denied
    return duration_minutes, interval_seconds, publish_reel


def manual_capture_request_error(text: str) -> str | None:
    """Explain incomplete manual capture requests before the AI fallback."""
    normalized = text.lower().strip()
    if not re.search(r"\b(time[ -]?lapse|timelapse)\b", normalized):
        return None
    if not re.search(r"\b(start|run|begin|launch|do|make|create|take|capture|set up)\b", normalized):
        return None
    if re.search(
        r"\b(?:do not|don't|dont|never)\s+"
        r"(?:start|run|begin|launch|do|make|create|take|capture|set up)\b",
        normalized,
    ):
        return None

    has_duration = bool(re.search(
        r"\b(?:for\s+)?\d+(?:\.\d+)?[\s-]*"
        r"(?:hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
        normalized,
    ))
    has_cadence = bool(re.search(
        r"\b\d+(?:\.\d+)?[\s-]*(?:seconds?|secs?|sec|s)\b",
        normalized,
    ))
    if has_duration and has_cadence:
        return None
    missing = []
    if not has_duration:
        missing.append("duration, such as 2 hours")
    if not has_cadence:
        missing.append("cadence, such as every 2 seconds")
    return "I can prepare that manual timelapse, but I still need " + " and ".join(missing) + "."


def manual_timelapse_refusal(duration_minutes: float, interval_seconds: float) -> str | None:
    if duration_minutes <= 0 or duration_minutes > MAX_MANUAL_CAPTURE_MINUTES:
        return "Manual capture duration must be greater than zero and no longer than 12 hours."
    if interval_seconds < v6_config.MIN_INTERVAL_SECONDS:
        return f"Manual capture cadence must be at least {v6_config.MIN_INTERVAL_SECONDS:g} seconds."
    if interval_seconds <= v6_config.SHUTTER_PULSE_SECONDS:
        return "Manual capture cadence must be longer than the shutter relay pulse."

    active = active_camera_jobs()
    if active:
        return "Manual timelapse refused because capture or post-processing is already active."
    if camera_is_usb_storage():
        return "Manual timelapse refused because the Olympus is in USB storage mode."

    output_root = v6_config.DEFAULT_OUTPUT_ROOT
    if not output_root.parent.is_mount():
        return f"Manual timelapse refused because the output drive is not mounted: {output_root.parent}"

    now = datetime.now().astimezone()
    capture_end = now + timedelta(minutes=duration_minutes)
    next_mission = next_scheduled_mission()
    if next_mission and capture_end >= next_mission.preflight_dt:
        return (
            "Manual timelapse refused because it would overlap the next scheduled preflight at "
            f"{next_mission.preflight_dt.strftime('%H:%M')}."
        )
    return None


def human_duration(seconds: float) -> str:
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def prepare_manual_timelapse(
    duration_minutes: float,
    interval_seconds: float,
    publish_reel: bool = False,
) -> str:
    refusal = manual_timelapse_refusal(duration_minutes, interval_seconds)
    if refusal:
        return refusal

    now = datetime.now().astimezone()
    max_frames = max(1, math.floor(duration_minutes * 60 / interval_seconds))
    video_seconds = max_frames / MANUAL_OUTPUT_FPS
    payload = {
        "status": "awaiting_confirmation",
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=MANUAL_CONFIRMATION_MINUTES)).isoformat(timespec="seconds"),
        "duration_minutes": duration_minutes,
        "interval_seconds": interval_seconds,
        "max_frames": max_frames,
        "output_fps": MANUAL_OUTPUT_FPS,
        "video_seconds": video_seconds,
        "publish_reel": bool(publish_reel),
    }
    v6_config.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PENDING_MANUAL_CAPTURE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(PENDING_MANUAL_CAPTURE)

    return (
        "Manual timelapse ready for confirmation:\n\n"
        f"- Type: General\n"
        f"- Capture time: {human_duration(duration_minutes * 60)}\n"
        f"- Cadence: every {interval_seconds:g}s\n"
        f"- Expected triggers: up to {max_frames:,}\n"
        f"- Resulting video: approximately {human_duration(video_seconds)} at {MANUAL_OUTPUT_FPS:g} fps\n"
        f"- Camera: wake and autofocus once at startup\n"
        f"- Storage: {v6_config.DEFAULT_OUTPUT_ROOT}\n"
        f"- Instagram: {'Reel only' if publish_reel else 'disabled'}\n\n"
        f"Reply **Confirm start** within {MANUAL_CONFIRMATION_MINUTES} minutes to begin, or **Cancel**."
    )


def confirm_manual_timelapse() -> str:
    pending = read_json(PENDING_MANUAL_CAPTURE)
    if pending.get("status") != "awaiting_confirmation":
        return "There is no manual timelapse awaiting confirmation."
    try:
        expires_at = datetime.fromisoformat(str(pending["expires_at"]))
        duration_minutes = float(pending["duration_minutes"])
        interval_seconds = float(pending["interval_seconds"])
        publish_reel = bool(pending.get("publish_reel", False))
    except (KeyError, TypeError, ValueError):
        PENDING_MANUAL_CAPTURE.unlink(missing_ok=True)
        return "The pending manual timelapse request was invalid and has been discarded."
    if datetime.now().astimezone() > expires_at:
        PENDING_MANUAL_CAPTURE.unlink(missing_ok=True)
        return "The pending manual timelapse confirmation expired. Please request it again."

    PENDING_MANUAL_CAPTURE.unlink(missing_ok=True)
    return start_manual_timelapse(duration_minutes, interval_seconds, publish_reel=publish_reel)


def cancel_manual_timelapse() -> str:
    if not PENDING_MANUAL_CAPTURE.exists():
        return "There is no manual timelapse awaiting confirmation."
    PENDING_MANUAL_CAPTURE.unlink(missing_ok=True)
    return "The pending manual timelapse was cancelled. Nothing was started."


def start_manual_timelapse(
    duration_minutes: float,
    interval_seconds: float,
    *,
    publish_reel: bool = False,
) -> str:
    refusal = manual_timelapse_refusal(duration_minutes, interval_seconds)
    if refusal:
        return refusal

    preflight_key = f"manual-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
    camera_ok, camera_message = ensure_camera_alive(preflight_key)
    if not camera_ok:
        return f"Manual timelapse refused: {camera_message}"

    max_frames = max(1, math.floor(duration_minutes * 60 / interval_seconds))
    now = datetime.now().astimezone()
    command = [
        sys.executable,
        str(v6_config.PROJECT_ROOT / "timelapser_v6.py"),
        "--mode", "general",
        "--interval", str(interval_seconds),
        "--max-frames", str(max_frames),
        "--max-run-minutes", str(duration_minutes),
        "--defer-postprocess",
    ]
    if publish_reel:
        command.append("--publish-instagram-reel")
    v6_config.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_CAPTURE_LOG.open("a", encoding="utf-8") as log:
        log.write(
            f"\n{now.isoformat(timespec='seconds')} DIRECTOR MANUAL START: "
            f"duration={duration_minutes:g}m interval={interval_seconds:g}s frames={max_frames}\n"
        )
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=str(v6_config.PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    time.sleep(0.75)
    if process.poll() is not None:
        return (
            "The manual timelapse process exited during startup. "
            f"Check {MANUAL_CAPTURE_LOG} for details."
        )
    return (
        f"Started a general {duration_minutes:g}-minute timelapse at {interval_seconds:g}-second "
        f"cadence (up to {max_frames:,} frames, PID {process.pid}). "
        "Post-processing is deferred and "
        + ("only the Reel will publish to Instagram." if publish_reel else "Instagram publishing is disabled.")
    )


def request_power_action(action: str, *, force: bool) -> str:
    helper_action = {
        ("reboot", False): "request",
        ("reboot", True): "force",
        ("shutdown", False): "shutdown",
        ("shutdown", True): "force-shutdown",
    }[(action, force)]
    try:
        result = subprocess.run(
            ["sudo", "-n", str(REBOOT_HELPER), helper_action],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"The {action} helper timed out, so I cannot confirm the request."
    except OSError as exc:
        return f"The {action} helper could not be started: {exc}"

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        if action == "reboot":
            return "Raspberry Pi reboot scheduled. The dashboard should return after boot."
        return "Raspberry Pi shutdown scheduled. Physical power is required to start it again."
    if result.returncode == 3:
        return (
            f"I refused to {action} because capture or post-processing is active. "
            f"Say 'force {action} the Pi' only if interrupting it is intentional."
        )
    if result.returncode == 4:
        return "A reboot or shutdown is already scheduled."
    return f"The {action} request failed: {output or f'exit {result.returncode}'}"


def run_director_preflight() -> str:
    active = active_camera_jobs()
    if active:
        return "Preflight refused while capture or post-processing is active."

    checks: list[str] = []
    ok = True
    output_root = v6_config.DEFAULT_OUTPUT_ROOT
    output_mount = output_root.parent
    if output_mount.is_mount():
        free_gb = shutil.disk_usage(output_root).free / 1024**3
        checks.append(f"OK external output drive: {free_gb:.1f} GB free")
    else:
        ok = False
        checks.append(f"FAIL external output drive is not mounted: {output_mount}")

    if camera_is_usb_storage():
        ok = False
        checks.append("FAIL camera is already in USB storage mode")
    else:
        checks.append("OK camera is released from USB storage")

    try:
        import gpiozero  # noqa: F401
        checks.append(
            f"OK relays available: AF GPIO{v6_config.FOCUS_GPIO}, "
            f"RLS GPIO{v6_config.SHUTTER_GPIO}"
        )
    except ImportError:
        ok = False
        checks.append("FAIL gpiozero is unavailable")

    checks.append("OK camera USB left untouched; physical capture is not tested during preflight")

    heading = "PREFLIGHT PASSED" if ok else "PREFLIGHT ATTENTION NEEDED"
    return heading + "\n\n" + "\n".join(f"- {line}" for line in checks)


def handle_director_command(text: str) -> str | None:
    normalized = text.lower().strip()
    if re.search(r"\b(cancel|discard|never mind|nevermind)\b", normalized) and PENDING_MANUAL_CAPTURE.exists():
        return cancel_manual_timelapse()
    if PENDING_MANUAL_CAPTURE.exists() and (
        normalized in {"confirm", "confirm start", "yes", "yes start", "start it", "proceed"}
        or re.search(r"\b(go ahead|confirm(?: and)? start|yes,? start it)\b", normalized)
    ):
        return confirm_manual_timelapse()
    manual_capture = parse_manual_capture_request(text)
    if manual_capture is not None:
        return prepare_manual_timelapse(*manual_capture)
    manual_capture_error = manual_capture_request_error(text)
    if manual_capture_error is not None:
        return manual_capture_error
    if re.search(r"\bpre[ -]?flight\b", normalized) and re.search(
        r"\b(check|test|run|now)\b", normalized
    ):
        return run_director_preflight()
    if re.search(r"\b(auto\s*focus|autofocus|focus camera|focus the camera)\b", normalized):
        ok, message = manual_camera_action("autofocus")
        return message if ok else f"I could not autofocus: {message}"
    if re.search(r"\b(take|capture|shoot|trigger)\b", normalized) and re.search(
        r"\b(photo|picture|shot|frame)\b", normalized
    ):
        ok, message = capture_and_download_test_shot()
        return message if ok else f"I could not take a photograph: {message}"

    if re.search(r"\b(eject|unmount|disconnect|unplug|release|disk mode)\b", normalized) and re.search(
        r"\b(camera|olympus)\b", normalized
    ):
        ok, message = release_camera_usb()
        return message if ok else f"I could not release the camera: {message}"

    if re.search(r"\b(pause|disable|suspend)\b", normalized) and re.search(
        r"\b(schedule|scheduled|automatic|timelapse)\b", normalized
    ):
        return set_scheduler_paused(True, text)
    if re.search(r"\b(resume|enable|unpause)\b", normalized) and re.search(
        r"\b(schedule|scheduled|automatic|timelapse)\b", normalized
    ):
        return set_scheduler_paused(False, text)

    force = bool(re.search(r"\b(force|anyway|even if busy)\b", normalized))
    if re.search(r"\b(reboot|restart)\b", normalized) and re.search(
        r"\b(pi|raspberry|system|machine)\b", normalized
    ):
        return request_power_action("reboot", force=force)
    if re.search(r"\b(shut\s*down|shutdown|power\s*off|turn\s*off)\b", normalized) and re.search(
        r"\b(pi|raspberry|system|machine)\b", normalized
    ):
        return request_power_action("shutdown", force=force)
    return None


def director_context(run_dir: Path, manifest: dict, post: dict, live: dict) -> str:
    climate, climate_error = enclosure_climate()
    next_mission = next_scheduled_mission()
    context = {
        "clock": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scheduler_paused": v6_config.SCHEDULER_PAUSE_FILE.exists(),
        "active_camera_jobs": active_camera_jobs(),
        "selected_run": str(run_dir),
        "manifest": manifest,
        "postprocess": post,
        "live_postprocess": live,
        "enclosure_climate": climate,
        "enclosure_sensor_error": climate_error,
        "pi_cpu_temperature_c": cpu_temperature_c(),
        "next_mission": ({
            "key": next_mission.key,
            "mode": next_mission.mode,
            "preflight": next_mission.preflight_dt.isoformat(),
            "capture_start": next_mission.start_dt.isoformat(),
            "capture_end": next_mission.end_dt.isoformat(),
        } if next_mission else None),
    }
    return json.dumps(context, indent=2, default=str)


def answer_director_question(question: str, run_dir: Path, manifest: dict, post: dict, live: dict) -> str:
    command_answer = handle_director_command(question)
    if command_answer is not None:
        return command_answer

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "The Director's OpenAI API key is not available, but its operational commands still work."

    try:
        from openai import OpenAI

        response = OpenAI(api_key=api_key).responses.create(
            model="gpt-5.6-luna",
            instructions="""You are the Timelapser V6 Director for Roy's Raspberry Pi 5 and Olympus E-M5 Mark III.

V6 triggers autofocus and shutter through isolated wired relays. It cannot see live images, exposure settings, camera mode, SD capacity, or focus confirmation. Image and EXIF analysis becomes available only after USB import. Use only the supplied state for factual claims. Explain missing evidence plainly and be concise.

Operational changes are handled before this AI call by deterministic, guarded functions. Never claim that you changed hardware, files, schedules, services, GPIO, USB, or system power. You may explain what the operator should ask the Director to do. Camera USB release must affect only the discovered Olympus MEGA4 port and never the WD drive.""",
            input=f"CURRENT V6 STATE:\n{director_context(run_dir, manifest, post, live)}\n\nOPERATOR:\n{question}",
        )
        return response.output_text
    except Exception as exc:
        return f"Director request failed: {type(exc).__name__}: {exc}"


st.title("Timelapser V6")
st.caption("Wired camera control. Image analysis begins after capture, not during it.")

if hasattr(st, "fragment"):
    st.fragment(run_every=5)(render_primary_status)()
else:
    render_primary_status()

st.subheader("Enclosure climate")
if hasattr(st, "fragment"):
    st.fragment(run_every=60)(render_enclosure_climate)()
else:
    render_enclosure_climate()

paused = v6_config.SCHEDULER_PAUSE_FILE.exists()
with st.sidebar:
    st.subheader("Capture scheduler")
    if paused:
        st.warning("Paused")
    else:
        st.success("Enabled")
    if st.button("Resume" if paused else "Pause", use_container_width=True):
        if paused:
            v6_config.SCHEDULER_PAUSE_FILE.unlink(missing_ok=True)
        else:
            v6_config.SCHEDULER_PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
            v6_config.SCHEDULER_PAUSE_FILE.write_text('{"paused": true}\n', encoding="utf-8")
        st.rerun()
    st.caption("Camera Wi-Fi is not used.")
    st.subheader("Camera USB")
    if st.button("Release camera USB", use_container_width=True):
        ok, message = release_camera_usb()
        if ok:
            st.success(message)
        else:
            st.error(message)
    st.caption("Releases only the discovered Olympus MEGA4 port.")

available = runs()
if not available:
    st.info(f"No V6 wired runs yet under {output_root()}")
    st.stop()

names = [path.name for path in available]
selected = st.selectbox("Run", names, index=0, label_visibility="collapsed")
run_dir = available[names.index(selected)]
manifest = live_manifest(run_dir)
post = read_json(run_dir / "postprocess_workflow_summary.json")
live = read_json(run_dir / "postprocess_live_status.json")
st.subheader("Run details")
render_capture_progress(run_dir)

st.subheader("Camera control")
control_cols = st.columns(3)
control_cols[0].metric("Capture path", "Wired relay")
control_cols[1].metric("Autofocus", "Wake + focus" if manifest.get("autofocus_once") else "Skipped")
control_cols[2].metric("Live images", "Off")
st.caption(
    f"AF GPIO {manifest.get('focus_gpio', 'not configured')} | "
    f"RLS GPIO {manifest.get('shutter_gpio', 'not configured')} | "
    f"last trigger {read_json(v6_config.ACTIVE_RUN).get('last_trigger_time', 'not available')}"
)
manual_cols = st.columns(2)
manual_controls_locked = bool(active_camera_jobs())
if manual_cols[0].button(
    "Autofocus",
    use_container_width=True,
    disabled=manual_controls_locked,
):
    with st.spinner("Focusing..."):
        ok, message = manual_camera_action("autofocus")
    if ok:
        st.success(message)
    else:
        st.error(message)
if manual_cols[1].button(
    "Take one shot",
    use_container_width=True,
    disabled=manual_controls_locked,
):
    with st.spinner("Waking camera, taking and downloading test shot..."):
        ok, message = capture_and_download_test_shot()
    if ok:
        st.success(message)
    else:
        st.error(message)
if manual_controls_locked:
    st.caption("Manual AF and shutter controls are locked while capture or post-processing is active.")
st.caption(
    "A test shot fires the shutter, waits for exposure and writing, briefly pulses AF to clear "
    "the remote state, then uses MEGA4 USB storage to download and release the newest JPEG."
)

purge_stale_test_shot()
test_shot = read_json(TEST_SHOT_STATE)
if test_shot.get("status") == "complete" and TEST_SHOT_IMAGE.exists():
    st.image(
        str(TEST_SHOT_IMAGE),
        caption=(
            f"Latest test shot: {test_shot.get('source_name', 'JPEG')} | "
            f"downloaded {test_shot.get('completed_at', 'unknown time')}"
        ),
        use_container_width=True,
    )
    if st.button("Hide test shot", use_container_width=True):
        clear_test_shot_cache()
        st.rerun()
elif test_shot.get("status") == "failed":
    st.warning(f"Latest test-shot download failed: {test_shot.get('error', 'unknown error')}")
    if st.button("Dismiss test-shot error", use_container_width=True):
        clear_test_shot_cache()
        st.rerun()

st.subheader("Post-run workflow")
if hasattr(st, "fragment"):
    st.fragment(run_every=5)(render_postprocess_panel)(run_dir)
else:
    render_postprocess_panel(run_dir)

# V6 withholds exposure and brightness history until rendering and publishing
# succeed. Optional delivery to the Mac must not suppress completed-run data.
if completed_workflow(post):
    telemetry_path = run_dir / "telemetry.csv"
    if telemetry_path.exists():
        data = pd.read_csv(telemetry_path)
        st.subheader("Completed-run image data")
        graph_cols = [name for name in ("median", "actual_iso", "actual_shutter_seconds", "actual_aperture") if name in data]
        if graph_cols:
            st.line_chart(data[graph_cols], height=260)
        st.caption("Extracted from full JPEGs after USB import; rendering and publishing are complete.")
else:
    st.info("Exposure and brightness graphs unlock after download, rendering, and upload all complete.")

st.subheader("Apple Photos")
clean_reel = run_dir / f"{run_dir.name}_final_instagram-reel_clean.mp4"
photos_request = read_json(run_dir / "photos_import_request.json")
request_active = photos_request.get("status") in {
    "requested", "processing", "waiting_for_unlock",
}
if st.button(
    "Download clean video to Mac Photos",
    use_container_width=True,
    disabled=not clean_reel.exists() or request_active,
):
    queue_photos_import(run_dir, clean_reel)
    st.rerun()
if not clean_reel.exists():
    st.caption("The clean reel will become available after rendering completes.")
elif photos_request:
    photos_status = str(photos_request.get("status") or "unknown").replace("_", " ").title()
    st.caption(f"{photos_status}: {photos_request.get('message', '')}")
else:
    st.caption("Queues this selected run; the Mac downloads and imports it when awake.")

with st.expander("Run files"):
    st.code(str(run_dir), language=None)
    st.json({"manifest": manifest, "postprocess": post})

st.divider()
chat_key = "v6_director_messages"
if chat_key not in st.session_state:
    st.session_state[chat_key] = []

with st.expander("Talk to the AI Director", expanded=True):
    st.caption(
        "Ask about V6 state, or request guarded scheduler, camera USB, reboot, and shutdown actions."
    )
    for message in st.session_state[chat_key]:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    with st.form("v6_director_chat", clear_on_submit=True):
        question = st.text_area(
            "Message",
            placeholder="Ask what the system is doing, or say 'release the camera from disk mode'...",
            height=90,
        )
        submitted = st.form_submit_button("Send")

    if submitted and question.strip():
        question = question.strip()
        st.session_state[chat_key].append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        answer = answer_director_question(question, run_dir, manifest, post, live)
        st.session_state[chat_key].append({"role": "assistant", "content": answer})
        with st.chat_message("assistant"):
            st.markdown(answer)
