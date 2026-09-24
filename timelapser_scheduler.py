#!/usr/bin/env python3
"""
timelapser_scheduler.py
=======================

Persistent scheduler for Timelapser V5 sunrise/sunset missions.

Usage:
    python3 timelapser_scheduler.py --status
    python3 timelapser_scheduler.py --once
    python3 timelapser_scheduler.py

Recommended unattended operation:
    run this script as a systemd service.

The scheduler:
- imports a human-editable chronological mission list from schedule.py
- calculates start/end from event time + configured offsets
- performs a preflight before capture
- launches timelapser_v5.py
- records persistent event state in scheduler_state.json
- can start late after reboot if an event is still active
- never marks an event complete merely because its nominal start passed
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("Python 3.9+ required for zoneinfo.", file=sys.stderr)
    raise

ROOT = Path(__file__).resolve().parent
SCHEDULE_FILE = ROOT / "schedule.py"
STATE_FILE = ROOT / "scheduler_state.json"
LOG_FILE = ROOT / "scheduler.log"
PREFLIGHT_ALERT_FILE = ROOT / "control" / "director_preflight_alert.json"

POLL_SECONDS = 20
MISSION_RETRY_DELAYS_SECONDS = (30, 60, 120)
MAX_MISSION_RETRIES = len(MISSION_RETRY_DELAYS_SECONDS)
HOME_WIFI_PROFILE = "netplan-wlan0-_A29-01"
CAMERA_WIFI_PROFILE = "E-M5MKIII-P-BJ8A00203"


def log(msg: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{stamp}  {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_preflight_alert(m: "Mission", checks: list[str], ok: bool) -> dict[str, Any]:
    issues = [line for line in checks if line.startswith(("WARN ", "FAIL "))]
    payload = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mission_key": m.key,
        "mission_mode": m.mode,
        "capture_start": m.start_dt.isoformat(),
        "capture_end": m.end_dt.isoformat(),
        "ready": bool(ok and not issues),
        "scheduler_ok": bool(ok),
        "issues": issues,
        "checks": checks,
    }
    PREFLIGHT_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PREFLIGHT_ALERT_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(PREFLIGHT_ALERT_FILE)
    return payload


def load_schedule_module():
    spec = importlib.util.spec_from_file_location("timelapser_user_schedule", SCHEDULE_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import schedule file: {SCHEDULE_FILE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@dataclass
class Mission:
    key: str
    date: str
    event: str
    event_time: str
    enabled: bool
    mode: str
    start_offset_minutes: int
    end_offset_minutes: int
    interval: float
    iso: int
    aperture: float
    shutter: float
    transfer_mode: str
    max_frames: int | None
    download_fullres_after: bool
    render_after: bool
    copy_videos_to_mac_after: bool
    mac_video_dest: str
    notes: str
    event_dt: datetime
    start_dt: datetime
    end_dt: datetime
    preflight_dt: datetime


def build_missions(mod) -> list[Mission]:
    tz = ZoneInfo(mod.TIMEZONE)
    defaults = mod.DEFAULTS
    missions: list[Mission] = []

    seen = set()
    for raw in mod.SCHEDULE:
        event = str(raw["event"]).strip().lower()
        if event not in defaults:
            raise ValueError(f"Unknown event type: {event!r}")

        cfg = dict(defaults[event])
        cfg.update(raw)

        date_s = str(cfg["date"])
        event_time = str(cfg["event_time"])
        event_dt = datetime.strptime(f"{date_s} {event_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        start_dt = event_dt + timedelta(minutes=float(cfg["start_offset_minutes"]))
        end_dt = event_dt + timedelta(minutes=float(cfg["end_offset_minutes"]))
        preflight_dt = start_dt - timedelta(minutes=float(mod.PREFLIGHT_MINUTES_BEFORE_START))

        key = f"{date_s}_{event}_{event_time.replace(':', '')}"
        if key in seen:
            raise ValueError(f"Duplicate schedule key: {key}")
        seen.add(key)

        missions.append(
            Mission(
                key=key,
                date=date_s,
                event=event,
                event_time=event_time,
                enabled=bool(cfg.get("enabled", True)),
                mode=str(cfg.get("mode", event)),
                start_offset_minutes=int(cfg["start_offset_minutes"]),
                end_offset_minutes=int(cfg["end_offset_minutes"]),
                interval=float(cfg["interval"]),
                iso=int(cfg["iso"]),
                aperture=float(cfg["aperture"]),
                shutter=float(cfg["shutter"]),
                transfer_mode=str(cfg.get("transfer_mode", "thumbnail")),
                max_frames=(None if cfg.get("max_frames") in (None, "") else int(cfg["max_frames"])),
                download_fullres_after=bool(cfg.get("download_fullres_after", False)),
                render_after=bool(cfg.get("render_after", False)),
                copy_videos_to_mac_after=bool(cfg.get("copy_videos_to_mac_after", False)),
                mac_video_dest=str(cfg.get("mac_video_dest", "")),
                notes=str(cfg.get("notes", "")),
                event_dt=event_dt,
                start_dt=start_dt,
                end_dt=end_dt,
                preflight_dt=preflight_dt,
            )
        )

    missions.sort(key=lambda m: m.event_dt)
    return missions


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"events": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"events": {}}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def update_event_state(state: dict[str, Any], key: str, **values) -> None:
    state.setdefault("events", {}).setdefault(key, {}).update(values)
    save_state(state)


def human_td(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def expected_frames(m: Mission, actual_start: datetime | None = None) -> int:
    start = actual_start or m.start_dt
    secs = max(0.0, (m.end_dt - start).total_seconds())
    nominal = int(secs // m.interval)
    if m.max_frames is not None:
        nominal = min(nominal, m.max_frames)
    return nominal


def port_open(host: str, port: int, timeout=0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_preflight(mod, m: Mission) -> tuple[bool, list[str]]:
    notes = []
    ok = True

    python = Path(mod.PYTHON)
    script = Path(mod.TIMELAPSER_SCRIPT)

    if python.exists():
        notes.append(f"OK python: {python}")
    else:
        ok = False
        notes.append(f"FAIL python missing: {python}")

    if script.exists():
        notes.append(f"OK timelapser script: {script}")
    else:
        ok = False
        notes.append(f"FAIL timelapser script missing: {script}")

    output_mount = Path(getattr(mod, "OUTPUT_MOUNT", ROOT))
    output_root = Path(getattr(mod, "OUTPUT_ROOT", ROOT))
    if hasattr(mod, "OUTPUT_MOUNT") and not output_mount.is_mount():
        ok = False
        notes.append(f"FAIL external output drive is not mounted: {output_mount}")
    else:
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            notes.append(f"OK output root: {output_root}")
        except Exception as exc:
            ok = False
            notes.append(f"FAIL output root unavailable: {type(exc).__name__}: {exc}")

    # Disk sanity: require at least 2 GB free on the configured output filesystem.
    try:
        du = shutil.disk_usage(output_root)
        free_gb = du.free / (1024**3)
        if free_gb >= 2:
            notes.append(f"OK output disk free: {free_gb:.1f} GB")
        else:
            ok = False
            notes.append(f"FAIL low disk space: {free_gb:.1f} GB")
    except Exception as exc:
        notes.append(f"WARN disk check failed: {exc}")

    # Follow NetworkManager profiles instead of volatile wlan numbers. Each
    # profile is bound to the intended adapter's permanent MAC address.
    camera_wifi_helper = Path("/usr/local/sbin/timelapser-camera-wifi")
    camera_interface = ""
    camera_connected = False
    home_interface = ""
    if shutil.which("nmcli"):
        try:
            active = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in active.stdout.splitlines():
                name, _, device = line.partition(":")
                if name == HOME_WIFI_PROFILE and device:
                    home_interface = device
                if name == CAMERA_WIFI_PROFILE and device:
                    camera_interface = device
                    camera_connected = True
        except (OSError, subprocess.TimeoutExpired):
            camera_connected = False

    if home_interface:
        notes.append(f"OK home Wi-Fi connected on {home_interface}")
    else:
        ok = False
        notes.append(f"FAIL home Wi-Fi profile is not active: {HOME_WIFI_PROFILE}")

    if camera_connected:
        notes.append(f"OK Olympus Wi-Fi connected on {camera_interface}")
    elif camera_wifi_helper.exists():
        try:
            result = subprocess.run(
                ["sudo", "-n", str(camera_wifi_helper), "connect"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                active = subprocess.run(
                    ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in active.stdout.splitlines():
                    name, _, device = line.partition(":")
                    if name == CAMERA_WIFI_PROFILE and device:
                        camera_interface = device
                        break
                notes.append(f"OK Olympus Wi-Fi connected on {camera_interface}")
            else:
                ok = False
                detail = (result.stderr or result.stdout).strip().splitlines()
                notes.append(f"FAIL Olympus Wi-Fi activation failed: {detail[-1] if detail else result.returncode}")
        except Exception as exc:
            ok = False
            notes.append(f"FAIL Olympus Wi-Fi activation failed: {type(exc).__name__}: {exc}")
    else:
        ok = False
        notes.append("FAIL camera Wi-Fi helper is not installed")

    # Olympus-safe informational reachability check.  The camera root URL is
    # not a normal web page and can misleadingly fail; get_caminfo.cgi is a
    # real Olympus endpoint and does not change capture mode.
    try:
        import urllib.request
        req = urllib.request.Request("http://192.168.0.10/get_caminfo.cgi", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            payload = resp.read(256)
            notes.append(f"OK Olympus camera API reachable ({resp.status}, {len(payload)}+ bytes)")
    except Exception as exc:
        ok = False
        notes.append(f"FAIL Olympus camera API not currently reachable: {type(exc).__name__}")

    return ok, notes


def build_command(mod, m: Mission, now: datetime) -> list[str]:
    remaining = max(0, int((m.end_dt - now).total_seconds()))
    max_run_minutes = max(1.0, remaining / 60.0)
    max_by_time = max(1, int(remaining // m.interval))

    if m.max_frames is not None:
        max_frames = min(max_by_time, m.max_frames)
    else:
        max_frames = max_by_time

    # These argument names match the current V5 CLI where known from the project.
    # Exposure values are supplied explicitly for scheduler determinism.
    cmd = [
        mod.PYTHON,
        mod.TIMELAPSER_SCRIPT,
        "--mode", m.mode,
        "--interval", str(m.interval),
        "--max-frames", str(max_frames),
        "--max-run-minutes", str(max_run_minutes),
        "--transfer-mode", m.transfer_mode,
        "--iso", str(m.iso),
        "--aperture", str(m.aperture),
        "--shutter", str(m.shutter),
    ]
    if m.download_fullres_after:
        cmd.append("--post-download-fullres")
    if m.render_after:
        cmd.append("--post-render")
    if m.copy_videos_to_mac_after:
        cmd.append("--post-copy-videos-to-mac")
    if m.mac_video_dest:
        cmd.extend(["--post-mac-video-dest", m.mac_video_dest])
    return cmd


def run_mission(mod, m: Mission, state: dict[str, Any], now: datetime) -> int:
    mission_started = time.monotonic()
    log(f"MISSION START {m.key} — planned {m.start_dt.isoformat()} -> {m.end_dt.isoformat()}")
    ok, checks = run_preflight(mod, m)
    write_preflight_alert(m, checks, ok)
    for line in checks:
        log(f"PREFLIGHT {m.key}: {line}")

    if not ok:
        update_event_state(
            state,
            m.key,
            status="preflight_failed",
            last_preflight=datetime.now().astimezone().isoformat(),
            preflight_checks=checks,
        )
        log(f"MISSION ABORT {m.key}: fatal preflight failure")
        return 20

    cmd = build_command(mod, m, now)
    update_event_state(
        state,
        m.key,
        status="running",
        actual_started=datetime.now().astimezone().isoformat(),
        command=cmd,
        notes=m.notes,
    )
    log("$ " + " ".join(repr(x) if " " in x else x for x in cmd))

    output_root = Path(getattr(mod, "OUTPUT_ROOT", Path(mod.TIMELAPSER_SCRIPT).parent))
    proc = subprocess.Popen(cmd, cwd=str(output_root))
    rc = proc.wait()

    ended = datetime.now().astimezone()
    elapsed_seconds = time.monotonic() - mission_started
    if rc == 0:
        status = "complete"
        retry_count = int(state.get("events", {}).get(m.key, {}).get("retry_count", 0))
        update_event_state(
            state,
            m.key,
            status=status,
            returncode=rc,
            actual_ended=ended.isoformat(),
            elapsed_seconds=elapsed_seconds,
            retry_count=retry_count,
            next_retry_at=None,
        )
    else:
        previous = state.get("events", {}).get(m.key, {})
        retry_count = int(previous.get("retry_count", 0)) + 1
        if retry_count <= MAX_MISSION_RETRIES and ended < m.end_dt:
            delay = MISSION_RETRY_DELAYS_SECONDS[retry_count - 1]
            next_retry = ended + timedelta(seconds=delay)
            status = "failed_retry_wait"
            update_event_state(
                state,
                m.key,
                status=status,
                returncode=rc,
                actual_ended=ended.isoformat(),
                elapsed_seconds=elapsed_seconds,
                retry_count=retry_count,
                next_retry_at=next_retry.isoformat(),
            )
            log(f"MISSION RETRY {m.key}: attempt {retry_count}/{MAX_MISSION_RETRIES} in {delay}s")
        else:
            status = "failed_terminal"
            update_event_state(
                state,
                m.key,
                status=status,
                returncode=rc,
                actual_ended=ended.isoformat(),
                elapsed_seconds=elapsed_seconds,
                retry_count=retry_count,
                next_retry_at=None,
            )
    log(
        f"MISSION END {m.key}: returncode={rc} status={status} "
        f"elapsed={elapsed_seconds:.1f}s"
    )

    return rc


def status_report(mod, missions: list[Mission], state: dict[str, Any]) -> None:
    tz = ZoneInfo(mod.TIMEZONE)
    now = datetime.now(tz)
    print("=" * 72)
    print("TIMELAPSER SCHEDULER STATUS")
    print("=" * 72)
    print(f"Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Schedule:     {SCHEDULE_FILE}")
    print(f"State:        {STATE_FILE}")
    print()

    enabled = [m for m in missions if m.enabled and m.end_dt > now]
    if not enabled:
        print("No future/active enabled missions.")
        return

    for i, m in enumerate(enabled[:8], start=1):
        evstate = state.get("events", {}).get(m.key, {})
        status = evstate.get("status", "pending")
        if m.start_dt <= now < m.end_dt:
            timing = f"ACTIVE — {human_td((m.end_dt-now).total_seconds())} remaining"
        elif now < m.preflight_dt:
            timing = f"in {human_td((m.preflight_dt-now).total_seconds())} to preflight"
        elif now < m.start_dt:
            timing = f"PREFLIGHT WINDOW — starts in {human_td((m.start_dt-now).total_seconds())}"
        else:
            timing = "ended"

        print(f"{i}. {m.key} [{status}]")
        print(f"   Event:     {m.event_dt.strftime('%a %d %b %Y %H:%M')}")
        print(f"   Preflight: {m.preflight_dt.strftime('%H:%M')}")
        print(f"   Capture:   {m.start_dt.strftime('%H:%M')} -> {m.end_dt.strftime('%H:%M')}")
        print(f"   Interval:  {m.interval:g}s")
        print(f"   Exposure:  ISO {m.iso}  f/{m.aperture:g}  {m.shutter:g}s")
        print(f"   Expected:  ~{expected_frames(m)} frames")
        post = []
        if m.download_fullres_after:
            post.append("download full JPEGs")
        if m.render_after:
            post.append("render videos")
        if m.copy_videos_to_mac_after:
            post.append("copy videos to Mac")
        print(f"   Post-run:  {', '.join(post) if post else 'off'}")
        print(f"   Timing:    {timing}")
        if m.notes:
            print(f"   Notes:     {m.notes}")
        print()


def scheduler_tick(mod, missions: list[Mission], state: dict[str, Any]) -> bool:
    """Run at most one mission. Return True if a mission was launched."""
    tz = ZoneInfo(mod.TIMEZONE)
    now = datetime.now(tz)

    for m in missions:
        if not m.enabled:
            continue

        evstate = state.get("events", {}).get(m.key, {})
        status = evstate.get("status", "pending")
        if status in {"complete", "running", "failed_terminal"}:
            continue

        if status == "failed_retry_wait":
            retry_count = int(evstate.get("retry_count", 0))
            if retry_count >= MAX_MISSION_RETRIES:
                update_event_state(state, m.key, status="failed_terminal", next_retry_at=None)
                continue
            next_retry_raw = evstate.get("next_retry_at")
            if next_retry_raw:
                try:
                    next_retry = datetime.fromisoformat(next_retry_raw)
                    if now < next_retry:
                        continue
                except Exception:
                    pass

        # Event fully expired.
        if now >= m.end_dt:
            if status not in {"missed", "failed", "preflight_failed"}:
                update_event_state(
                    state,
                    m.key,
                    status="missed",
                    marked_missed_at=now.isoformat(),
                )
                log(f"MISSION MISSED {m.key}: event window has ended")
            continue

        # Preflight window.
        if m.preflight_dt <= now < m.start_dt:
            last_pf = evstate.get("last_preflight")
            if not last_pf:
                ok, checks = run_preflight(mod, m)
                write_preflight_alert(m, checks, ok)
                for line in checks:
                    log(f"PREFLIGHT {m.key}: {line}")
                update_event_state(
                    state,
                    m.key,
                    status=("armed" if ok else "preflight_failed"),
                    last_preflight=now.isoformat(),
                    preflight_checks=checks,
                )
            return False

        # Normal start or late start after reboot.
        if m.start_dt <= now < m.end_dt:
            remaining_minutes = (m.end_dt - now).total_seconds() / 60
            late_allowed = bool(getattr(mod, "START_LATE_IF_STILL_ACTIVE", True))
            min_remaining = float(getattr(mod, "MIN_REMAINING_MINUTES_TO_START", 10))

            if now > m.start_dt and not late_allowed:
                update_event_state(state, m.key, status="missed")
                continue

            if remaining_minutes < min_remaining:
                update_event_state(
                    state,
                    m.key,
                    status="missed",
                    reason=f"Only {remaining_minutes:.1f} minutes remained",
                )
                continue

            run_mission(mod, m, state, now)
            return True

    return False


def reconcile_stale_running_state(state: dict[str, Any]) -> None:
    """A newly started scheduler cannot own a child from a prior process/boot.

    run_mission() blocks while its child is alive, so any persisted 'running'
    status seen during scheduler startup is stale by definition.  Mark it
    interrupted so the normal active-window late-start logic can recover it.
    """
    changed = False
    now = datetime.now().astimezone().isoformat()
    for key, ev in state.get("events", {}).items():
        if ev.get("status") == "running":
            ev["status"] = "interrupted"
            ev["interrupted_detected_at"] = now
            ev["reason"] = "Scheduler restarted while mission state was running"
            changed = True
            log(f"RECOVERY {key}: stale running state -> interrupted")
    if changed:
        save_state(state)


def main() -> int:
    p = argparse.ArgumentParser(description="Timelapser V5 mission scheduler")
    p.add_argument("--status", action="store_true", help="Show next missions and exit")
    p.add_argument("--once", action="store_true", help="Run one scheduler tick and exit")
    args = p.parse_args()

    mod = load_schedule_module()
    missions = build_missions(mod)
    state = load_state()

    if args.status:
        status_report(mod, missions, state)
        return 0

    reconcile_stale_running_state(state)

    log("Timelapser scheduler started.")
    log(f"Loaded {len(missions)} missions from {SCHEDULE_FILE}")

    if args.once:
        scheduler_tick(mod, missions, state)
        return 0

    try:
        while True:
            # Reload schedule each tick so user edits take effect without restart.
            try:
                mod = load_schedule_module()
                missions = build_missions(mod)
            except Exception as exc:
                log(f"SCHEDULE LOAD ERROR: {type(exc).__name__}: {exc}")
                time.sleep(POLL_SECONDS)
                continue

            scheduler_tick(mod, missions, state)
            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        log("Scheduler stopped by user.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
