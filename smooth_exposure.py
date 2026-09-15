#!/usr/bin/env python3
"""
Smooth short-term exposure flicker in a timelapse JPEG sequence.

This is intentionally conservative: originals are never modified, analysis is
written to CSV/JSON, and image output happens only when --apply is passed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

JPG_EXTENSIONS = {".jpg", ".jpeg"}


@dataclass(frozen=True)
class FrameMetric:
    index: int
    path: Path
    luminance: float
    target_luminance: float
    correction_ratio: float
    correction_stops: float


def natural_sort_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.stem.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def find_frames(input_dir: Path, limit: int | None = None) -> list[Path]:
    frames = [
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in JPG_EXTENSIONS
    ]
    frames.sort(key=natural_sort_key)
    if limit is not None:
        frames = frames[:limit]
    return frames


def parse_roi(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [float(part.strip()) for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("--roi must be x,y,w,h")
    x, y, w, h = parts
    if w <= 0 or h <= 0:
        raise ValueError("--roi width and height must be positive")
    return x, y, w, h


def crop_roi(arr: np.ndarray, roi: tuple[float, float, float, float] | None) -> np.ndarray:
    if roi is None:
        return arr
    height, width = arr.shape[:2]
    x, y, w, h = roi
    if max(x, y, w, h) <= 1.0:
        x0 = int(round(x * width))
        y0 = int(round(y * height))
        x1 = int(round((x + w) * width))
        y1 = int(round((y + h) * height))
    else:
        x0 = int(round(x))
        y0 = int(round(y))
        x1 = int(round(x + w))
        y1 = int(round(y + h))
    x0 = max(0, min(width - 1, x0))
    y0 = max(0, min(height - 1, y0))
    x1 = max(x0 + 1, min(width, x1))
    y1 = max(y0 + 1, min(height, y1))
    return arr[y0:y1, x0:x1]


def srgb_to_linear(arr: np.ndarray) -> np.ndarray:
    return np.where(arr <= 0.04045, arr / 12.92, ((arr + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(arr: np.ndarray) -> np.ndarray:
    return np.where(arr <= 0.0031308, arr * 12.92, 1.055 * np.power(arr, 1.0 / 2.4) - 0.055)


def luminance_from_rgb(arr: np.ndarray) -> np.ndarray:
    return 0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]


def open_rgb(path: Path, max_edge: int | None = None) -> Image.Image:
    image = Image.open(path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max_edge and max(image.size) > max_edge:
        image.thumbnail((max_edge, max_edge), Image.Resampling.BICUBIC)
    return image


def measure_luminance(
    path: Path,
    *,
    max_edge: int,
    roi: tuple[float, float, float, float] | None,
    metric: str,
    trim_percent: float,
) -> float:
    image = open_rgb(path, max_edge=max_edge)
    arr = np.asarray(image, dtype=np.float32) / 255.0
    arr = crop_roi(arr, roi)
    lum = luminance_from_rgb(srgb_to_linear(arr)).reshape(-1)

    if trim_percent > 0:
        lo = np.percentile(lum, trim_percent)
        hi = np.percentile(lum, 100.0 - trim_percent)
        trimmed = lum[(lum >= lo) & (lum <= hi)]
        if trimmed.size:
            lum = trimmed

    if metric == "median":
        return float(np.median(lum))
    if metric == "mean":
        return float(np.mean(lum))
    raise ValueError(f"Unknown metric: {metric}")


def smooth_curve(values: np.ndarray, window: int, passes: int) -> np.ndarray:
    if window < 3 or len(values) < 3:
        return values.copy()
    if window % 2 == 0:
        window += 1
    window = min(window, len(values) if len(values) % 2 else len(values) - 1)
    if window < 3:
        return values.copy()

    curve = values.astype(np.float64).copy()
    kernel = np.ones(window, dtype=np.float64) / float(window)
    pad = window // 2
    for _ in range(max(1, passes)):
        padded = np.pad(curve, pad_width=pad, mode="edge")
        curve = np.convolve(padded, kernel, mode="valid")
    return curve


def compute_metrics(
    frames: list[Path],
    *,
    max_edge: int,
    roi: tuple[float, float, float, float] | None,
    metric: str,
    trim_percent: float,
    window: int,
    passes: int,
    strength: float,
    max_stops: float,
    deadband_stops: float,
) -> list[FrameMetric]:
    luminance_values: list[float] = []
    for index, path in enumerate(frames, start=1):
        luminance_values.append(
            measure_luminance(
                path,
                max_edge=max_edge,
                roi=roi,
                metric=metric,
                trim_percent=trim_percent,
            )
        )
        if index % 50 == 0 or index == len(frames):
            print(f"[{index}/{len(frames)}] analyzed")

    luminances = np.array(luminance_values, dtype=np.float64)
    target = smooth_curve(luminances, window=window, passes=passes)

    raw_ratio = np.divide(target, luminances, out=np.ones_like(target), where=luminances > 1e-9)
    raw_stops = np.log2(np.clip(raw_ratio, 1e-9, None))
    correction_stops = raw_stops * strength
    correction_stops = np.clip(correction_stops, -max_stops, max_stops)
    correction_stops[np.abs(correction_stops) < deadband_stops] = 0.0
    correction_ratio = np.power(2.0, correction_stops)

    return [
        FrameMetric(
            index=index + 1,
            path=path,
            luminance=float(luminances[index]),
            target_luminance=float(target[index]),
            correction_ratio=float(correction_ratio[index]),
            correction_stops=float(correction_stops[index]),
        )
        for index, path in enumerate(frames)
    ]


def apply_correction(
    source: Path,
    destination: Path,
    ratio: float,
    quality: int,
    *,
    optimize: bool,
    output_max_edge: int | None,
) -> None:
    image = open_rgb(source, max_edge=None)
    if output_max_edge and max(image.size) > output_max_edge:
        image.thumbnail((output_max_edge, output_max_edge), Image.Resampling.LANCZOS)
    arr = np.asarray(image, dtype=np.float32) / 255.0
    linear = srgb_to_linear(arr)
    linear = np.clip(linear * ratio, 0.0, 1.0)
    corrected = np.clip(linear_to_srgb(linear) * 255.0, 0.0, 255.0).astype(np.uint8)
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(corrected).save(
        destination,
        format="JPEG",
        quality=quality,
        optimize=optimize,
        subsampling=0,
    )


def write_csv(path: Path, metrics: list[FrameMetric]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "index",
                "filename",
                "luminance",
                "target_luminance",
                "correction_ratio",
                "correction_stops",
            ],
        )
        writer.writeheader()
        for item in metrics:
            writer.writerow(
                {
                    "index": item.index,
                    "filename": item.path.name,
                    "luminance": f"{item.luminance:.10f}",
                    "target_luminance": f"{item.target_luminance:.10f}",
                    "correction_ratio": f"{item.correction_ratio:.8f}",
                    "correction_stops": f"{item.correction_stops:.6f}",
                }
            )


def write_svg_plot(path: Path, metrics: list[FrameMetric]) -> None:
    if not metrics:
        return
    width, height = 1200, 520
    margin = 54
    xs = np.arange(len(metrics), dtype=np.float64)
    actual = np.array([item.luminance for item in metrics], dtype=np.float64)
    target = np.array([item.target_luminance for item in metrics], dtype=np.float64)
    values = np.concatenate([actual, target])
    lo, hi = float(values.min()), float(values.max())
    if math.isclose(lo, hi):
        hi = lo + 1.0

    def points(series: np.ndarray) -> str:
        result = []
        denom = max(1.0, len(series) - 1.0)
        for x, value in zip(xs, series):
            px = margin + (x / denom) * (width - margin * 2)
            py = height - margin - ((value - lo) / (hi - lo)) * (height - margin * 2)
            result.append(f"{px:.1f},{py:.1f}")
        return " ".join(result)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#111318"/>
  <text x="{margin}" y="34" fill="#f4f4f4" font-family="Menlo, monospace" font-size="18">Timelapser exposure smoothing</text>
  <text x="{margin}" y="{height - 18}" fill="#aab" font-family="Menlo, monospace" font-size="12">frames: {len(metrics)}  luminance: {lo:.5f} to {hi:.5f}</text>
  <polyline points="{points(actual)}" fill="none" stroke="#ff6b6b" stroke-width="1.4" opacity="0.8"/>
  <polyline points="{points(target)}" fill="none" stroke="#4dabf7" stroke-width="2.2" opacity="0.95"/>
  <text x="{width - 260}" y="32" fill="#ff6b6b" font-family="Menlo, monospace" font-size="13">actual</text>
  <text x="{width - 180}" y="32" fill="#4dabf7" font-family="Menlo, monospace" font-size="13">target</text>
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {secs:.0f}s"
    hours, mins = divmod(minutes, 60)
    return f"{int(hours)}h {int(mins)}m"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Directory containing source JPEG frames")
    parser.add_argument("--output", help="Directory for corrected JPEG frames")
    parser.add_argument("--apply", action="store_true", help="Write corrected JPEGs")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing output directory")
    parser.add_argument("--limit", type=int, help="Process only the first N frames, useful for fast tests")
    parser.add_argument("--window", type=int, default=31, help="Smoothing window in frames, default: 31")
    parser.add_argument("--passes", type=int, default=2, help="Number of smoothing passes, default: 2")
    parser.add_argument("--strength", type=float, default=0.85, help="Correction strength 0..1, default: 0.85")
    parser.add_argument("--max-stops", type=float, default=0.25, help="Max correction per frame in stops, default: 0.25")
    parser.add_argument("--deadband-stops", type=float, default=0.01, help="Ignore tiny corrections, default: 0.01")
    parser.add_argument("--analysis-max-edge", type=int, default=512, help="Downsample long edge for analysis, default: 512")
    parser.add_argument("--metric", choices=["median", "mean"], default="median", help="Luminance statistic")
    parser.add_argument("--trim-percent", type=float, default=2.0, help="Ignore darkest/brightest percent for analysis")
    parser.add_argument("--roi", help="Optional analysis ROI as x,y,w,h. Fractions 0..1 or pixels.")
    parser.add_argument("--quality", type=int, default=95, help="Output JPEG quality, default: 95")
    parser.add_argument("--jpeg-optimize", action="store_true", help="Use slower JPEG optimization")
    parser.add_argument("--output-max-edge", type=int, help="Resize corrected frames so long edge is at most N pixels")
    args = parser.parse_args()

    input_dir = Path(args.input).expanduser().resolve()
    if not input_dir.exists():
        print(f"Input directory does not exist: {input_dir}", file=sys.stderr)
        return 2

    frames = find_frames(input_dir, limit=args.limit)
    if len(frames) < 3:
        print(f"Need at least 3 JPEG frames, found {len(frames)}", file=sys.stderr)
        return 2

    output_dir = Path(args.output).expanduser().resolve() if args.output else input_dir.parent / "frames_smoothed_luma"
    report_dir = output_dir if args.apply else input_dir.parent / "exposure_smoothing_analysis"

    if args.apply:
        if output_dir.exists() and args.overwrite:
            shutil.rmtree(output_dir)
        if output_dir.exists() and any(output_dir.iterdir()):
            print(f"Output directory is not empty: {output_dir}", file=sys.stderr)
            print("Use --overwrite to replace it.", file=sys.stderr)
            return 2
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        report_dir.mkdir(parents=True, exist_ok=True)

    roi = parse_roi(args.roi)
    started = time.monotonic()
    print(f"Analyzing {len(frames)} JPEG frames")
    print(f"Input:  {input_dir}")
    print(f"Output: {output_dir if args.apply else '(dry run; no images written)'}")
    print(f"Window: {args.window}, passes: {args.passes}, max correction: +/-{args.max_stops} stops")

    metrics = compute_metrics(
        frames,
        max_edge=args.analysis_max_edge,
        roi=roi,
        metric=args.metric,
        trim_percent=args.trim_percent,
        window=args.window,
        passes=args.passes,
        strength=args.strength,
        max_stops=args.max_stops,
        deadband_stops=args.deadband_stops,
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "exposure_smoothing_report.csv"
    svg_path = report_dir / "exposure_smoothing_curve.svg"
    json_path = report_dir / "exposure_smoothing_summary.json"
    write_csv(csv_path, metrics)
    write_svg_plot(svg_path, metrics)

    corrections = np.array([item.correction_stops for item in metrics], dtype=np.float64)
    nonzero = int(np.sum(np.abs(corrections) > 0))
    summary = {
        "input": str(input_dir),
        "output": str(output_dir) if args.apply else None,
        "frames": len(metrics),
        "applied": bool(args.apply),
        "window": args.window,
        "passes": args.passes,
        "strength": args.strength,
        "max_stops": args.max_stops,
        "deadband_stops": args.deadband_stops,
        "metric": args.metric,
        "trim_percent": args.trim_percent,
        "roi": args.roi,
        "quality": args.quality,
        "jpeg_optimize": args.jpeg_optimize,
        "output_max_edge": args.output_max_edge,
        "frames_with_correction": nonzero,
        "max_abs_correction_stops": float(np.max(np.abs(corrections))),
        "mean_abs_correction_stops": float(np.mean(np.abs(corrections))),
        "report_csv": str(csv_path),
        "curve_svg": str(svg_path),
    }
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    if args.apply:
        for item in metrics:
            destination = output_dir / item.path.name
            if math.isclose(item.correction_ratio, 1.0) and not args.output_max_edge:
                shutil.copy2(item.path, destination)
            else:
                apply_correction(
                    item.path,
                    destination,
                    item.correction_ratio,
                    args.quality,
                    optimize=args.jpeg_optimize,
                    output_max_edge=args.output_max_edge,
                )
            if item.index % 50 == 0 or item.index == len(metrics):
                elapsed = time.monotonic() - started
                rate = item.index / elapsed if elapsed > 0 else 0.0
                remaining = (len(metrics) - item.index) / rate if rate > 0 else 0.0
                print(f"[{item.index}/{len(metrics)}] {format_duration(elapsed)} elapsed, ETA {format_duration(remaining)}")

    elapsed = time.monotonic() - started
    print(f"Done in {format_duration(elapsed)}")
    print(f"Frames with correction: {nonzero}/{len(metrics)}")
    print(f"Max correction: {summary['max_abs_correction_stops']:.4f} stops")
    print(f"Report: {csv_path}")
    print(f"Curve:  {svg_path}")
    if not args.apply:
        print("Dry run only. Add --apply to write corrected frames.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
