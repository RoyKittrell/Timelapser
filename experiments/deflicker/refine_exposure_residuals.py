#!/usr/bin/env python3
"""Refine Timelapser exposure smoothing using common-mode regional residuals.

The first-stage Pi correction is retained. A short temporal median is computed
independently for a grid of image regions; only changes shared by most regions
become a second-stage global exposure correction. This rejects local cloud and
traffic motion better than a single whole-frame luminance curve.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from correct_discrete_exposure_states import correct_one


def measure_regions(path: str, analysis_edge: int = 480) -> list[float]:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((analysis_edge, analysis_edge), Image.Resampling.LANCZOS)
        rgb = np.asarray(image, dtype=np.float32) / 255.0
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    luminance = 0.2126 * linear[:, :, 0] + 0.7152 * linear[:, :, 1] + 0.0722 * linear[:, :, 2]
    height, width = luminance.shape
    values: list[float] = []
    # Inset each cell to reduce sensitivity to objects crossing cell boundaries.
    for row in range(3):
        for column in range(3):
            y0, y1 = row * height // 3, (row + 1) * height // 3
            x0, x1 = column * width // 3, (column + 1) * width // 3
            dy, dx = max(1, (y1 - y0) // 10), max(1, (x1 - x0) // 10)
            cell = luminance[y0 + dy : y1 - dy, x0 + dx : x1 - dx]
            values.append(float(np.median(cell)))
    return values


def temporal_median(values: np.ndarray, window: int) -> np.ndarray:
    radius = window // 2
    padded = np.pad(values, ((radius, radius), (0, 0)), mode="edge")
    return np.median(
        np.stack([padded[offset : offset + len(values)] for offset in range(window)]),
        axis=0,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pi_smoothed_frames", type=Path)
    parser.add_argument("original_frames", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--window", type=int, default=5)
    parser.add_argument("--max-residual-stops", type=float, default=0.15)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--output-max-edge", type=int, default=3840)
    parser.add_argument("--quality", type=int, default=95)
    args = parser.parse_args()
    if args.window < 3 or args.window % 2 == 0:
        raise SystemExit("--window must be an odd integer of at least 3")

    pi_frames = sorted(args.pi_smoothed_frames.glob("frame_*.jpg"))
    originals = sorted(args.original_frames.glob("frame_*.jpg"))
    if not pi_frames or len(pi_frames) != len(originals):
        raise SystemExit(f"Frame mismatch: {len(pi_frames)} Pi-smoothed, {len(originals)} originals")

    first_stage: dict[str, float] = {}
    with (args.pi_smoothed_frames / "exposure_smoothing.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            first_stage[row["filename"]] = float(row["correction_stops"])

    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        regions = np.asarray(list(pool.map(measure_regions, map(str, pi_frames))), dtype=np.float64)
    log_regions = np.log2(np.clip(regions, 1e-8, None))
    targets = temporal_median(log_regions, args.window)
    region_residuals = targets - log_regions
    residual = np.median(region_residuals, axis=1)
    residual = np.clip(residual, -args.max_residual_stops, args.max_residual_stops)

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    tasks = []
    with (output / "residual_exposure_correction.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("index", "filename", "first_stage_stops", "residual_stops", "combined_stops"))
        for index, (source, correction) in enumerate(zip(originals, residual), start=1):
            initial = first_stage.get(source.name, 0.0)
            combined = initial + float(correction)
            writer.writerow((index, source.name, initial, float(correction), combined))
            tasks.append(
                (str(source), str(output / source.name), combined, args.output_max_edge, args.quality)
            )

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for index, _ in enumerate(pool.map(correct_one, tasks), start=1):
            if index % 100 == 0 or index == len(tasks):
                print(f"[{index}/{len(tasks)}] prepared", flush=True)

    before_step = np.diff(log_regions, axis=0)
    after_step = np.diff(log_regions + residual[:, None], axis=0)
    common_before = np.median(np.abs(before_step), axis=1)
    common_after = np.median(np.abs(after_step), axis=1)
    summary = {
        "method": "pi_first_stage_plus_common_mode_regional_residual",
        "frames": len(tasks),
        "regions": int(regions.shape[1]),
        "window": args.window,
        "max_residual_stops": args.max_residual_stops,
        "residual_min_stops": float(residual.min()),
        "residual_max_stops": float(residual.max()),
        "residual_abs_p95_stops": float(np.percentile(np.abs(residual), 95)),
        "adjacent_common_step_p95_before_stops": float(np.percentile(common_before, 95)),
        "adjacent_common_step_p95_after_stops": float(np.percentile(common_after, 95)),
        "elapsed_seconds": time.monotonic() - started,
    }
    (output / "residual_exposure_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
