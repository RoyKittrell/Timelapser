#!/usr/bin/env python3
"""Parallel high-resolution exposure smoothing for production renders."""

from __future__ import annotations

import argparse
import csv
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

import smooth_exposure as smoothing


def measure(task: tuple[str, int]) -> float:
    path, max_edge = task
    return smoothing.measure_luminance(
        Path(path),
        max_edge=max_edge,
        roi=None,
        metric="median",
        trim_percent=2.0,
    )


def correct(task: tuple[str, str, float, int]) -> str:
    source, destination, ratio, max_edge = task
    smoothing.apply_correction(
        Path(source),
        Path(destination),
        ratio,
        95,
        optimize=False,
        output_max_edge=max_edge,
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--analysis-max-edge", type=int, default=512)
    parser.add_argument("--output-max-edge", type=int, default=3840)
    args = parser.parse_args()

    frames = smoothing.find_frames(args.input.resolve())
    if len(frames) < 3:
        raise SystemExit("Need at least three source frames")
    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        luminances = np.asarray(
            list(pool.map(measure, ((str(path), args.analysis_max_edge) for path in frames))),
            dtype=np.float64,
        )
    analyzed_at = time.monotonic()

    target = smoothing.smooth_curve(luminances, window=61, passes=3)
    ratio = np.divide(target, luminances, out=np.ones_like(target), where=luminances > 1e-9)
    stops = np.clip(np.log2(np.clip(ratio, 1e-9, None)), -0.25, 0.25)
    stops[np.abs(stops) < 0.01] = 0.0
    ratios = np.power(2.0, stops)

    with (output / "exposure_smoothing.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("index", "filename", "luminance", "target_luminance", "correction_stops"))
        for index, (path, value, wanted, correction) in enumerate(
            zip(frames, luminances, target, stops), start=1
        ):
            writer.writerow((index, path.name, value, wanted, correction))

    tasks = (
        (str(path), str(output / path.name), float(correction), args.output_max_edge)
        for path, correction in zip(frames, ratios)
    )
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for index, _ in enumerate(pool.map(correct, tasks), start=1):
            if index % 100 == 0 or index == len(frames):
                print(f"[{index}/{len(frames)}] corrected", flush=True)

    finished = time.monotonic()
    print(f"analysis_seconds={analyzed_at - started:.3f}")
    print(f"correction_seconds={finished - analyzed_at:.3f}")
    print(f"total_seconds={finished - started:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
