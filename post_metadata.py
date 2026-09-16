#!/usr/bin/env python3
"""Generate public-facing post metadata and captions for Timelapser runs."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astral import Observer
from astral.moon import phase as moon_phase
from astral.sun import azimuth, sun


LOCATION_NAME = "Kuala Lumpur, Malaysia"
SCENE_NAME = "118"
TIMEZONE = ZoneInfo("Asia/Kuala_Lumpur")
LATITUDE = 3.1390
LONGITUDE = 101.6869
OBSERVER = Observer(latitude=LATITUDE, longitude=LONGITUDE)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_telemetry_bounds(run_dir: Path) -> tuple[datetime | None, datetime | None, int | None]:
    path = run_dir / "telemetry.csv"
    if not path.exists():
        return None, None, None

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return None, None, None

    if not rows:
        return None, None, None

    def parse_dt(text: str | None) -> datetime | None:
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(str(text))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        return dt.astimezone(TIMEZONE)

    first = parse_dt(rows[0].get("time") or rows[0].get("timestamp"))
    last = parse_dt(rows[-1].get("time") or rows[-1].get("timestamp"))
    return first, last, len(rows)


def mode_from_run(run_dir: Path, summary: dict[str, Any]) -> str:
    mode = str(summary.get("mode") or "").strip().lower()
    if mode in {"sunrise", "sunset", "general"}:
        return mode
    for candidate in ("sunrise", "sunset", "general"):
        if f"_{candidate}_" in run_dir.name:
            return candidate
    return "timelapse"


def date_from_run_name(run_dir: Path) -> date | None:
    match = re.match(r"^(\d{8})_", run_dir.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except Exception:
        return None


def format_date(d: date) -> str:
    return d.strftime("%d %b %Y").lstrip("0")


def format_clock(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(TIMEZONE).strftime("%H:%M")


def solar_declination_deg(day: date) -> float:
    """NOAA-style solar declination approximation for the local noon date."""
    year_start = date(day.year, 1, 1)
    day_of_year = (day - year_start).days + 1
    leap_days = 366 if (day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0)) else 365
    gamma = 2 * math.pi / leap_days * (day_of_year - 1)
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    return math.degrees(decl)


def seasonal_markers(year: int) -> list[tuple[str, date]]:
    return [
        ("March equinox", date(year, 3, 20)),
        ("June solstice", date(year, 6, 21)),
        ("September equinox", date(year, 9, 23)),
        ("December solstice", date(year, 12, 21)),
    ]


def analemma_note(day: date) -> str:
    candidates: list[tuple[int, str, date]] = []
    for year in (day.year - 1, day.year, day.year + 1):
        for name, marker_date in seasonal_markers(year):
            candidates.append((abs((day - marker_date).days), name, marker_date))
    _, name, marker_date = min(candidates, key=lambda x: x[0])
    delta = (day - marker_date).days
    if delta == 0:
        return f"on the {name}"
    unit = "day" if abs(delta) == 1 else "days"
    if delta < 0:
        return f"{abs(delta)} {unit} before the {name}"
    return f"{delta} {unit} after the {name}"


def moon_phase_name(age: float) -> str:
    # Astral returns a moon age-like value from 0 to 27.99.
    if age < 1.84566:
        return "new moon"
    if age < 5.53699:
        return "waxing crescent"
    if age < 9.22831:
        return "first quarter"
    if age < 12.91963:
        return "waxing gibbous"
    if age < 16.61096:
        return "full moon"
    if age < 20.30228:
        return "waning gibbous"
    if age < 23.99361:
        return "last quarter"
    if age < 27.68493:
        return "waning crescent"
    return "new moon"


def moon_illumination_percent(age: float) -> float:
    # Astral's phase cycle is 0..~28. Convert to an approximate illuminated fraction.
    synodic = 29.530588853
    fraction = (1 - math.cos(2 * math.pi * (age / synodic))) / 2
    return max(0.0, min(100.0, fraction * 100))


def build_metadata(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    run_summary = read_json(run_dir / "run_summary.json")
    started, ended, frames = read_telemetry_bounds(run_dir)
    mode = mode_from_run(run_dir, run_summary)

    run_date = (started.date() if started else date_from_run_name(run_dir) or datetime.now(TIMEZONE).date())
    solar = sun(OBSERVER, date=run_date, tzinfo=TIMEZONE)
    event_dt = solar.get("sunrise" if mode == "sunrise" else "sunset") if mode in {"sunrise", "sunset"} else None
    event_azimuth = azimuth(OBSERVER, event_dt) if event_dt else None
    moon_age = float(moon_phase(run_date))
    moon_name = moon_phase_name(moon_age)
    moon_illum = moon_illumination_percent(moon_age)
    declination = solar_declination_deg(run_date)
    mode_title = "Sunrise" if mode == "sunrise" else "Sunset" if mode == "sunset" else "Timelapse"

    title = f"{SCENE_NAME} {mode_title} · {format_date(run_date)}"
    caption_lines = [
        title,
        LOCATION_NAME,
    ]
    if event_dt and event_azimuth is not None:
        caption_lines.append(f"{mode_title} {format_clock(event_dt)} · solar azimuth {event_azimuth:.1f}°")
    caption_lines.append(f"Solar declination {declination:+.1f}° · {analemma_note(run_date)}")
    caption_lines.append(f"Moon: {moon_name} · {moon_illum:.0f}% illuminated")
    if started and ended:
        capture = f"Captured {format_clock(started)}-{format_clock(ended)}"
        if frames:
            capture += f" · {frames} frames"
        caption_lines.append(capture)
    caption_lines.append("Olympus E-M5 Mark III · Raspberry Pi Timelapser V5")

    tags = "#timelapse #kualalumpur #sunrise #sunset #analemma #moonphase #olympus #raspberrypi #photography"
    caption = "\n".join(caption_lines) + "\n\n" + tags

    return {
        "title": title,
        "caption": caption,
        "location": LOCATION_NAME,
        "scene": SCENE_NAME,
        "mode": mode,
        "date": run_date.isoformat(),
        "event_time_local": event_dt.isoformat(timespec="seconds") if event_dt else "",
        "event_solar_azimuth_deg": round(event_azimuth, 2) if event_azimuth is not None else None,
        "solar_declination_deg": round(declination, 2),
        "analemma_note": analemma_note(run_date),
        "moon_phase": moon_name,
        "moon_phase_age": round(moon_age, 2),
        "moon_illumination_percent": round(moon_illum, 1),
        "captured_start_local": started.isoformat(timespec="seconds") if started else "",
        "captured_end_local": ended.isoformat(timespec="seconds") if ended else "",
        "frames": frames,
        "generated_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
    }


def write_post_files(run_dir: Path) -> dict[str, Any]:
    metadata = build_metadata(run_dir)
    run_dir = run_dir.expanduser().resolve()
    (run_dir / "post_metadata.json").write_text(
        json.dumps(metadata, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (run_dir / "caption.txt").write_text(metadata["caption"].rstrip() + "\n", encoding="utf-8")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Timelapser post metadata and caption files.")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    metadata = write_post_files(args.run_dir)
    print(json.dumps(metadata, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
