#!/usr/bin/env python3
"""Correct a repeatable discrete shutter-state brightness jump in JPEG frames.

Unlike the normal low-pass exposure smoother, this calibrates one fixed stop
offset from adjacent A-B-A shutter transitions and applies it only to frames in
the darker shutter state. Originals are never modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def calibrate(rows: list[dict[str, str]]) -> tuple[float, float, list[float]]:
    shutters = sorted({float(row["actual_shutter_seconds"]) for row in rows})
    if len(shutters) != 2:
        raise ValueError(f"Expected exactly two shutter states, found {shutters}")
    short, long = shutters
    offsets: list[float] = []
    for before, middle, after in zip(rows, rows[1:], rows[2:]):
        a = float(before["actual_shutter_seconds"])
        b = float(middle["actual_shutter_seconds"])
        c = float(after["actual_shutter_seconds"])
        if a != c or b == a:
            continue
        luminances = [float(row["median"]) for row in (before, middle, after)]
        if min(luminances) <= 0:
            continue
        residual = math.log2(luminances[1]) - (
            math.log2(luminances[0]) + math.log2(luminances[2])
        ) / 2.0
        # Store the brightness advantage of the long state over the short state.
        offsets.append(residual if b == long else -residual)
    if not offsets:
        raise ValueError("No A-B-A shutter transitions were available for calibration")
    return short, long, offsets


def srgb_to_linear(values: np.ndarray) -> np.ndarray:
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(values: np.ndarray) -> np.ndarray:
    return np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * np.power(values, 1.0 / 2.4) - 0.055,
    )


def correct_one(task: tuple[str, str, float, int, int]) -> str:
    source_name, destination_name, stops, max_edge, quality = task
    source = Path(source_name)
    destination = Path(destination_name)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        if max(image.size) > max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if stops:
            pixels = np.asarray(image, dtype=np.float32) / 255.0
            linear = srgb_to_linear(pixels)
            linear *= 2.0**stops
            corrected = linear_to_srgb(np.clip(linear, 0.0, 1.0))
            image = Image.fromarray(np.rint(np.clip(corrected, 0.0, 1.0) * 255.0).astype(np.uint8))
        image.save(destination, quality=quality, subsampling=0, optimize=False)
    return destination.name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("frames", type=Path)
    parser.add_argument("telemetry", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-max-edge", type=int, default=3840)
    parser.add_argument("--quality", type=int, default=95)
    args = parser.parse_args()

    rows = _rows(args.telemetry)
    frames = sorted(args.frames.glob("*.jpg")) + sorted(args.frames.glob("*.JPG"))
    if len(frames) != len(rows):
        raise SystemExit(f"Frame/telemetry mismatch: {len(frames)} JPEGs versus {len(rows)} rows")

    short, long, samples = calibrate(rows)
    correction_stops = statistics.median(samples)
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    tasks = []
    corrected_count = 0
    for frame, row in zip(frames, rows):
        shutter = float(row["actual_shutter_seconds"])
        correction = correction_stops if math.isclose(shutter, short, rel_tol=1e-6) else 0.0
        corrected_count += bool(correction)
        tasks.append((str(frame), str(output / frame.name), correction, args.output_max_edge, args.quality))

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for index, _ in enumerate(pool.map(correct_one, tasks), start=1):
            if index % 100 == 0 or index == len(tasks):
                print(f"[{index}/{len(tasks)}] prepared", flush=True)

    summary = {
        "method": "discrete_shutter_state_correction",
        "short_shutter_seconds": short,
        "long_shutter_seconds": long,
        "calibration_samples": len(samples),
        "correction_stops_for_short_state": correction_stops,
        "linear_light_multiplier_for_short_state": 2.0**correction_stops,
        "corrected_frames": corrected_count,
        "total_frames": len(frames),
        "output_max_edge": args.output_max_edge,
        "jpeg_quality": args.quality,
        "elapsed_seconds": time.monotonic() - started,
    }
    (output / "discrete_exposure_correction.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
