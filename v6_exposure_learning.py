#!/usr/bin/env python3
"""Build conservative sunrise/sunset exposure curves from completed V6 runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from v6_exposure_model import DEFAULT_CALIBRATION_PATH, EXPECTED_APERTURE

BIN_MINUTES = 2.0
MAX_RUNS_PER_MODE = 12
MIN_FRAMES_PER_RUN = 100
UPPER_PERCENTILE = 0.90
MODEL_SETTINGS_EPOCH = datetime.fromisoformat("2026-10-28T18:13:17+08:00")
EXPECTED_ISO_MIN = 200
EXPECTED_ISO_MAX = 400


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    left = math.floor(position)
    right = math.ceil(position)
    if left == right:
        return ordered[left]
    weight = position - left
    return ordered[left] * (1 - weight) + ordered[right] * weight


def valid_run_samples(run_dir: Path) -> tuple[str, list[tuple[float, float]]] | None:
    manifest = read_json(run_dir / "run_manifest.json")
    imported = read_json(run_dir / "v6_usb_import_summary.json")
    mode = str(manifest.get("mode") or "").lower()
    if mode not in {"sunrise", "sunset"} or imported.get("status") != "complete":
        return None
    try:
        event_time = datetime.fromisoformat(str(manifest["event_time"]))
        started_at = datetime.fromisoformat(str(manifest["started_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if started_at < MODEL_SETTINGS_EPOCH:
        return None

    samples: list[tuple[float, float]] = []
    try:
        with (run_dir / "telemetry.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    captured = datetime.fromisoformat(row["time"])
                    if captured.tzinfo is None:
                        captured = captured.replace(tzinfo=event_time.tzinfo)
                    shutter = float(row["actual_shutter_seconds"])
                    iso = float(row["actual_iso"])
                    aperture = float(row["actual_aperture"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (
                    shutter <= 0
                    or not EXPECTED_ISO_MIN <= iso <= EXPECTED_ISO_MAX
                    or abs(aperture - EXPECTED_APERTURE) > 0.35
                ):
                    continue
                offset = (captured - event_time).total_seconds() / 60.0
                samples.append((offset, shutter))
    except OSError:
        return None
    return (mode, samples) if len(samples) >= MIN_FRAMES_PER_RUN else None


def enforce_direction(mode: str, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if mode == "sunrise":
        for index in range(len(points) - 2, -1, -1):
            points[index] = (points[index][0], max(points[index][1], points[index + 1][1]))
    else:
        for index in range(1, len(points)):
            points[index] = (points[index][0], max(points[index][1], points[index - 1][1]))
    return points


def learn(runs_root: Path, output: Path) -> dict:
    candidates: dict[str, list[tuple[float, Path, list[tuple[float, float]]]]] = defaultdict(list)
    for run_dir in runs_root.iterdir():
        if not run_dir.is_dir():
            continue
        result = valid_run_samples(run_dir)
        if result:
            mode, samples = result
            candidates[mode].append((run_dir.stat().st_mtime, run_dir, samples))

    modes = {}
    for mode, runs in candidates.items():
        selected = sorted(runs, reverse=True)[:MAX_RUNS_PER_MODE]
        bins: dict[float, list[float]] = defaultdict(list)
        for _, _, samples in selected:
            for offset, shutter in samples:
                minute = round(offset / BIN_MINUTES) * BIN_MINUTES
                bins[minute].append(shutter)
        points = [(minute, percentile(values, UPPER_PERCENTILE)) for minute, values in sorted(bins.items())]
        modes[mode] = {
            "points": enforce_direction(mode, points),
            "run_ids": [run_dir.name for _, run_dir, _ in selected],
            "percentile": UPPER_PERCENTILE,
            "bin_minutes": BIN_MINUTES,
        }

    payload = {
        "version": 2,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "settings_epoch": MODEL_SETTINGS_EPOCH.isoformat(),
        "settings_profile": {
            "iso_min": EXPECTED_ISO_MIN,
            "iso_max": EXPECTED_ISO_MAX,
            "preferred_max_shutter_seconds": 2.0,
        },
        "modes": modes,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_root", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_CALIBRATION_PATH)
    args = parser.parse_args()
    print(json.dumps(learn(args.runs_root.expanduser(), args.output.expanduser()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
