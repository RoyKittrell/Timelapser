#!/usr/bin/env python3
"""Import the newest wired-capture JPEG sequence and build post-run telemetry."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
MIN_JPEG_BYTES = 50_000
CAPTURE_END_TOLERANCE = timedelta(minutes=2)


@dataclass(frozen=True)
class CameraImage:
    path: Path
    sequence_key: tuple[str, str]
    captured_at: datetime | None


def exif_value(image: Image.Image, tag: int):
    try:
        return image.getexif().get_ifd(0x8769).get(tag)
    except (AttributeError, KeyError):
        return None


def parse_exif_time(image: Image.Image) -> datetime | None:
    raw = exif_value(image, 36867) or image.getexif().get(306)
    try:
        return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S") if raw else None
    except ValueError:
        return None


def numeric_exif(value):
    if value is None:
        return ""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        numerator, denominator = value
        return float(numerator) / float(denominator) if denominator else ""
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return ""


def validate_jpeg(path: Path) -> None:
    if path.stat().st_size < MIN_JPEG_BYTES:
        raise ValueError(f"JPEG is unexpectedly small: {path}")
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            raise ValueError(f"JPEG start marker missing: {path}")
        handle.seek(-64, os.SEEK_END)
        if b"\xff\xd9" not in handle.read():
            raise ValueError(f"JPEG end marker missing: {path}")


def camera_images(sd_root: Path) -> list[CameraImage]:
    dcim = sd_root / "DCIM"
    if not dcim.is_dir():
        raise FileNotFoundError(f"No DCIM directory under {sd_root}")
    found = []
    for path in dcim.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in JPEG_EXTENSIONS:
            continue
        validate_jpeg(path)
        with Image.open(path) as image:
            captured = parse_exif_time(image)
        found.append(CameraImage(path, (path.parent.name.upper(), path.name.upper()), captured))
    return sorted(found, key=lambda item: item.sequence_key)


def image_metrics(path: Path) -> dict:
    with Image.open(path) as source:
        captured_at = parse_exif_time(source)
        exposure = exif_value(source, 33434)
        aperture = exif_value(source, 33437)
        iso = exif_value(source, 34855)
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((512, 512), Image.Resampling.BICUBIC)
        array = np.asarray(image, dtype=np.float32) / 255.0
    linear = np.where(array <= 0.04045, array / 12.92, ((array + 0.055) / 1.055) ** 2.4)
    luminance = 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]
    return {
        "time": captured_at.isoformat(sep=" ") if captured_at else "",
        "actual_iso": numeric_exif(iso),
        "actual_aperture": numeric_exif(aperture),
        "actual_shutter_seconds": numeric_exif(exposure),
        "median": float(np.median(luminance)),
        "p05": float(np.percentile(luminance, 5)),
        "p95": float(np.percentile(luminance, 95)),
    }


def copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(".jpg.part")
    with source.open("rb") as src, part.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(part, destination)
    validate_jpeg(destination)


def manifest_capture_window(manifest: dict) -> tuple[datetime, datetime]:
    try:
        started = datetime.fromisoformat(str(manifest["started_at"]))
        ended = datetime.fromisoformat(str(manifest["ended_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Run manifest lacks a valid capture start/end time") from exc
    # Olympus EXIF timestamps are local wall-clock values without a UTC offset.
    return started.replace(tzinfo=None), ended.replace(tzinfo=None) + CAPTURE_END_TOLERANCE


def import_run(run_dir: Path, sd_root: Path, *, dry_run: bool = False) -> dict:
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = int(manifest.get("completed_frames") or 0)
    if expected < 1:
        raise ValueError("Run manifest has no completed wired triggers")
    available = camera_images(sd_root)
    capture_started, capture_ended = manifest_capture_window(manifest)
    selected = sorted(
        (
            item for item in available
            if item.captured_at is not None and capture_started <= item.captured_at <= capture_ended
        ),
        key=lambda item: (item.captured_at, item.sequence_key),
    )
    if not selected:
        raise RuntimeError(
            "No camera JPEG has an EXIF capture time inside this run's capture window; "
            "refusing to import unrelated photographs"
        )
    if len(selected) > expected:
        raise RuntimeError(
            f"Capture window contains {len(selected)} JPEGs for {expected} relay triggers; "
            "refusing an ambiguous import"
        )

    frames_dir = run_dir / "frames_full_jpeg"
    staging_dir = run_dir / "frames_full_jpeg.importing"
    if not dry_run:
        shutil.rmtree(staging_dir, ignore_errors=True)
        staging_dir.mkdir(parents=True)
    rows = []
    for frame, item in enumerate(selected, start=1):
        destination = staging_dir / f"frame_{frame:06d}.jpg"
        if not dry_run:
            copy_atomic(item.path, destination)
            metrics = image_metrics(destination)
        else:
            metrics = image_metrics(item.path)
        rows.append({
            "frame": frame,
            "remote_jpg": str(item.path.relative_to(sd_root)).replace(os.sep, "/"),
            "source_basename": item.path.name,
            **metrics,
        })

    if not dry_run:
        fields = [
            "frame", "remote_jpg", "source_basename", "time", "actual_iso",
            "actual_aperture", "actual_shutter_seconds", "median", "p05", "p95",
        ]
        telemetry_tmp = run_dir / "telemetry.csv.importing"
        with telemetry_tmp.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        previous_dir = run_dir / "frames_full_jpeg.previous"
        shutil.rmtree(previous_dir, ignore_errors=True)
        if frames_dir.exists():
            frames_dir.replace(previous_dir)
        staging_dir.replace(frames_dir)
        telemetry_tmp.replace(run_dir / "telemetry.csv")
        shutil.rmtree(previous_dir, ignore_errors=True)
        summary = {
            "status": "complete",
            "selection_rule": "exif_capture_time_within_manifest_window",
            "relay_triggers": expected,
            "imported_frames": len(rows),
            "unmatched_triggers": expected - len(rows),
            "capture_window_start": capture_started.isoformat(sep=" "),
            "capture_window_end": capture_ended.isoformat(sep=" "),
            "first_source": str(selected[0].path),
            "last_source": str(selected[-1].path),
            "telemetry_created_after_capture": True,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        (run_dir / "v6_usb_import_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        return summary
    return {"status": "dry_run", "relay_triggers": expected, "matched_frames": len(rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a wired Timelapser V6 run from Olympus USB storage")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--sd-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = import_run(args.run_dir.resolve(), args.sd_root.resolve(), dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
