#!/usr/bin/env python3
"""Consolidate per-run capture, astronomy, air-quality and exposure metadata."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from post_metadata import build_metadata


CATALOG_FIELDS = (
    "run_id", "version", "mode", "date", "captured_start_local", "captured_end_local",
    "relay_fires", "actual_jpegs", "unmatched_fires", "event_time_local",
    "event_solar_azimuth_deg", "solar_declination_deg", "analemma_note",
    "moon_phase", "moon_phase_age", "moon_illumination_percent", "us_aqi",
    "iso_median", "aperture_median", "shutter_min_seconds", "shutter_median_seconds",
    "shutter_max_seconds", "catalogued_at",
)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def telemetry_statistics(path: Path) -> dict[str, float | None]:
    values: dict[str, list[float]] = {
        "actual_iso": [], "actual_aperture": [], "actual_shutter_seconds": [],
    }
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                for field in values:
                    try:
                        values[field].append(float(row[field]))
                    except (KeyError, TypeError, ValueError):
                        pass
    except OSError:
        pass
    shutters = values["actual_shutter_seconds"]
    return {
        "iso_median": statistics.median(values["actual_iso"]) if values["actual_iso"] else None,
        "aperture_median": statistics.median(values["actual_aperture"]) if values["actual_aperture"] else None,
        "shutter_min_seconds": min(shutters) if shutters else None,
        "shutter_median_seconds": statistics.median(shutters) if shutters else None,
        "shutter_max_seconds": max(shutters) if shutters else None,
    }


def build_context(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    manifest = read_json(run_dir / "run_manifest.json")
    imported = read_json(run_dir / "v6_usb_import_summary.json")
    astronomy = build_metadata(run_dir)
    air_quality = read_json(run_dir / "air_quality.json")
    relay_fires = int(manifest.get("completed_frames") or 0)
    actual_jpegs = int(imported.get("imported_frames") or astronomy.get("frames") or 0)
    context = {
        "run_id": run_dir.name,
        "version": manifest.get("version", ""),
        "mode": astronomy.get("mode", manifest.get("mode", "")),
        "date": astronomy.get("date", ""),
        "captured_start_local": astronomy.get("captured_start_local", ""),
        "captured_end_local": astronomy.get("captured_end_local", ""),
        "relay_fires": relay_fires or actual_jpegs,
        "actual_jpegs": actual_jpegs,
        "unmatched_fires": int(imported.get("unmatched_triggers") or max(0, relay_fires - actual_jpegs)),
        "event_time_local": astronomy.get("event_time_local", manifest.get("event_time", "")),
        "event_solar_azimuth_deg": astronomy.get("event_solar_azimuth_deg"),
        "solar_declination_deg": astronomy.get("solar_declination_deg"),
        "analemma_note": astronomy.get("analemma_note", ""),
        "moon_phase": astronomy.get("moon_phase", ""),
        "moon_phase_age": astronomy.get("moon_phase_age"),
        "moon_illumination_percent": astronomy.get("moon_illumination_percent"),
        "air_quality": air_quality,
        "us_aqi": air_quality.get("us_aqi"),
        "exposure": telemetry_statistics(run_dir / "telemetry.csv"),
        "catalogued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    context.update(context["exposure"])
    return context


def write_context(run_dir: Path) -> dict[str, Any]:
    context = build_context(run_dir)
    output = run_dir / "run_context.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(context, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(output)
    return context


def update_catalog(root: Path, contexts: list[dict[str, Any]]) -> Path:
    path = root / "timelapse_catalog.csv"
    rows: dict[str, dict[str, str]] = {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = {row["run_id"]: row for row in csv.DictReader(handle) if row.get("run_id")}
    except OSError:
        pass
    for context in contexts:
        rows[str(context["run_id"])] = {
            field: "" if context.get(field) is None else str(context.get(field, ""))
            for field in CATALOG_FIELDS
        }
    temporary = path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows.values(), key=lambda row: (row["date"], row["run_id"])))
    temporary.replace(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="One run directory, or the runs root with --all")
    parser.add_argument("--all", action="store_true", help="Backfill every run containing telemetry.csv")
    args = parser.parse_args()
    target = args.path.expanduser().resolve()
    run_dirs = sorted(path for path in target.iterdir() if path.is_dir() and (path / "telemetry.csv").exists()) if args.all else [target]
    contexts = [write_context(run_dir) for run_dir in run_dirs]
    root = target if args.all else target.parent
    catalog = update_catalog(root, contexts)
    print(json.dumps({"runs_catalogued": len(contexts), "catalog": str(catalog)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
