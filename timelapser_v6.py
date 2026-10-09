#!/usr/bin/env python3
"""Timelapser V6 wired capture runner.

V6 deliberately has no camera Wi-Fi, thumbnail download, live image analysis,
or exposure control. It focuses once, sends deterministic shutter pulses, and
records an auditable trigger timeline. Image ingest and EXIF analysis happen
only after capture has ended.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

import v6_config as config
from v6_intervalometer import WiredCamera


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def append_event(path: Path, kind: str, **details) -> None:
    record = {
        "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "monotonic": time.monotonic(),
        "event": kind,
        **details,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Timelapser V6 wired intervalometer capture")
    parser.add_argument("--mode", choices=("sunrise", "sunset", "general"), default="general")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--max-frames", type=int, default=None)
    parser.add_argument("--max-run-minutes", type=float, default=60.0)
    parser.add_argument("--output-root", type=Path, default=config.DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--focus-gpio", type=int, default=config.FOCUS_GPIO)
    parser.add_argument("--shutter-gpio", type=int, default=config.SHUTTER_GPIO)
    parser.add_argument("--focus-pulse", type=float, default=config.FOCUS_PULSE_SECONDS)
    parser.add_argument("--focus-settle", type=float, default=config.FOCUS_SETTLE_SECONDS)
    parser.add_argument(
        "--half-press-clear",
        type=float,
        default=config.HALF_PRESS_CLEAR_SECONDS,
    )
    parser.add_argument("--shutter-pulse", type=float, default=config.SHUTTER_PULSE_SECONDS)
    parser.add_argument("--simulate", action="store_true", help="Log timing without touching GPIO")
    parser.add_argument("--skip-autofocus", action="store_true")
    parser.add_argument("--defer-postprocess", action="store_true")
    return parser.parse_args()


def validate(args: argparse.Namespace) -> None:
    if args.interval < config.MIN_INTERVAL_SECONDS:
        raise ValueError(f"Interval must be at least {config.MIN_INTERVAL_SECONDS:g}s")
    if args.shutter_pulse >= args.interval:
        raise ValueError("Shutter pulse must be shorter than the capture interval")
    if args.max_frames is not None and args.max_frames < 1:
        raise ValueError("max-frames must be positive")
    if args.max_run_minutes <= 0:
        raise ValueError("max-run-minutes must be positive")
    if not args.simulate and (args.focus_gpio is None or args.shutter_gpio is None):
        raise ValueError(
            "Real capture is locked until TIMELAPSER_V6_FOCUS_GPIO and "
            "TIMELAPSER_V6_SHUTTER_GPIO (or both CLI options) are configured"
        )


def make_run_dir(args: argparse.Namespace) -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    suffix = "_simulated" if args.simulate else ""
    run_dir = args.output_root.expanduser() / f"{stamp}_{args.mode}_v6_wired{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def main() -> int:
    args = parse_args()
    try:
        validate(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    run_dir = make_run_dir(args)
    events = run_dir / "capture_events.jsonl"
    triggers = run_dir / "trigger_timeline.csv"
    stop = False

    def request_stop(signum, _frame) -> None:
        nonlocal stop
        stop = True
        append_event(events, "stop_requested", signal=signum)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if config.STOP_REQUEST.exists():
        config.STOP_REQUEST.unlink()

    started_at = datetime.now().astimezone()
    deadline = time.monotonic() + args.max_run_minutes * 60
    estimated_by_time = max(1, math.floor(args.max_run_minutes * 60 / args.interval))
    planned_frames = min(estimated_by_time, args.max_frames) if args.max_frames else estimated_by_time
    manifest = {
        "version": "6.0",
        "capture_backend": "wired_intervalometer",
        "mode": args.mode,
        "started_at": started_at.isoformat(timespec="seconds"),
        "interval_seconds": args.interval,
        "max_frames": planned_frames,
        "focus_gpio": args.focus_gpio,
        "shutter_gpio": args.shutter_gpio,
        "relay_active_low": config.RELAY_ACTIVE_LOW,
        "focus_pulse_seconds": args.focus_pulse,
        "half_press_clear_seconds": args.half_press_clear,
        "shutter_pulse_seconds": args.shutter_pulse,
        "autofocus_once": not args.skip_autofocus,
        "simulate": args.simulate,
        "postprocess_deferred": args.defer_postprocess,
        "status": "starting",
    }
    atomic_json(run_dir / "run_manifest.json", manifest)
    atomic_json(config.ACTIVE_RUN, {"run_dir": str(run_dir), **manifest})
    append_event(events, "run_started", planned_frames=planned_frames)

    rows: list[dict] = []
    status = "failed"
    error = ""
    try:
        with WiredCamera(
            args.focus_gpio if args.focus_gpio is not None else 0,
            args.shutter_gpio if args.shutter_gpio is not None else 1,
            active_low=config.RELAY_ACTIVE_LOW,
            simulate=args.simulate,
        ) as camera:
            shutter_attempted = False
            try:
                if not args.skip_autofocus:
                    append_event(events, "autofocus_started")
                    pulse = camera.autofocus_once(args.focus_pulse)
                    append_event(events, "autofocus_complete", pulse_seconds=pulse.duration_seconds)
                    time.sleep(args.focus_settle)

                first_due = time.monotonic()
                for frame_number in range(1, planned_frames + 1):
                    if stop or config.STOP_REQUEST.exists() or time.monotonic() >= deadline:
                        break
                    due = first_due + (frame_number - 1) * args.interval
                    remaining = due - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                    if stop or config.STOP_REQUEST.exists():
                        break
                    wall_started = datetime.now().astimezone()
                    shutter_attempted = True
                    pulse = camera.take_picture(args.shutter_pulse)
                    row = {
                        "frame": frame_number,
                        "trigger_time": wall_started.isoformat(timespec="milliseconds"),
                        "scheduled_monotonic": due,
                        "actual_monotonic": pulse.started_monotonic,
                        "schedule_lag_seconds": pulse.started_monotonic - due,
                        "pulse_seconds": pulse.duration_seconds,
                    }
                    rows.append(row)
                    append_event(events, "shutter_released", **row)
                    atomic_json(
                        config.ACTIVE_RUN,
                        {
                            "run_dir": str(run_dir),
                            **manifest,
                            "status": "capturing",
                            "completed_frames": len(rows),
                            "last_trigger_time": row["trigger_time"],
                        },
                    )
            finally:
                if shutter_attempted:
                    append_event(events, "half_press_clear_started")
                    try:
                        pulse = camera.clear_half_press(args.half_press_clear)
                    except Exception as clear_exc:
                        append_event(
                            events,
                            "half_press_clear_failed",
                            error=f"{type(clear_exc).__name__}: {clear_exc}",
                        )
                    else:
                        append_event(
                            events,
                            "half_press_clear_complete",
                            pulse_seconds=pulse.duration_seconds,
                        )
        status = "capture_complete"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        append_event(events, "capture_failed", error=error)
    finally:
        fieldnames = [
            "frame", "trigger_time", "scheduled_monotonic", "actual_monotonic",
            "schedule_lag_seconds", "pulse_seconds",
        ]
        with triggers.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        manifest.update(
            status=status,
            completed_frames=len(rows),
            ended_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            error=error,
        )
        atomic_json(run_dir / "run_manifest.json", manifest)
        atomic_json(config.ACTIVE_RUN, {"run_dir": str(run_dir), **manifest})
        if args.defer_postprocess and status == "capture_complete" and rows:
            auto_publish = args.mode in {"sunrise", "sunset"}
            atomic_json(
                run_dir / "postprocess_deferred.json",
                {
                    "version": "6.0",
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "run_dir": str(run_dir),
                    "status": "pending",
                    "publish_instagram": auto_publish,
                    "mode": args.mode,
                },
            )
        if config.STOP_REQUEST.exists():
            config.STOP_REQUEST.unlink()

    print(json.dumps({"run_dir": str(run_dir), **manifest}, indent=2))
    return 0 if status == "capture_complete" and rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
