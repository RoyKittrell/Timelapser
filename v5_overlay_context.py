"""Solar phase context for video overlays."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from astral import SunDirection
from astral.sun import blue_hour, golden_hour

from post_metadata import LATITUDE, LONGITUDE, OBSERVER, TIMEZONE


def _local_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed.astimezone(TIMEZONE)


def run_bounds(rows: list[dict]) -> tuple[datetime, datetime]:
    return _local_time(rows[0]["time"]), _local_time(rows[-1]["time"])


def solar_markers(rows: list[dict]) -> list[tuple[datetime, str]]:
    """Return only solar transitions crossed during the captured interval."""
    start, end = run_bounds(rows)
    markers = []
    day = start.date() - timedelta(days=1)
    while day <= end.date() + timedelta(days=1):
        for direction in (SunDirection.RISING, SunDirection.SETTING):
            try:
                golden = golden_hour(OBSERVER, day, direction, TIMEZONE)
                blue = blue_hour(OBSERVER, day, direction, TIMEZONE)
            except ValueError:
                continue
            if direction == SunDirection.SETTING:
                events = ((golden[0], "golden"), (blue[0], "blue"), (blue[1], "night"))
            else:
                events = ((blue[0], "night"), (golden[0], "blue"), (golden[1], "golden"))
            markers.extend((at, icon) for at, icon in events if start <= at <= end)
        day += timedelta(days=1)
    return sorted(markers)


def air_quality_for_run(run_dir: Path, rows: list[dict]) -> dict | None:
    """Cache the modeled US AQI nearest the capture midpoint for repeatable renders."""
    cache = run_dir / "air_quality.json"
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("source") == "Open-Meteo CAMS Global" and isinstance(saved.get("us_aqi"), (int, float)):
            return saved
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    start, end = run_bounds(rows)
    midpoint = start + (end - start) / 2
    params = urlencode({
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "us_aqi",
        "start_date": midpoint.date().isoformat(),
        "end_date": midpoint.date().isoformat(),
        "timezone": str(TIMEZONE),
        "domains": "cams_global",
    })
    url = f"https://air-quality-api.open-meteo.com/v1/air-quality?{params}"
    for attempt in range(3):
        try:
            with urlopen(url, timeout=8) as response:
                payload = json.load(response)
            hourly = payload["hourly"]
            samples = [
                (_local_time(at), value)
                for at, value in zip(hourly["time"], hourly["us_aqi"])
                if isinstance(value, (int, float)) and 0 <= value <= 1000
            ]
            sample_time, value = min(samples, key=lambda sample: abs(sample[0] - midpoint))
            if abs(sample_time - midpoint) > timedelta(hours=2):
                return None
            break
        except (OSError, ValueError, KeyError, TypeError):
            if attempt == 2:
                return None
            time.sleep(2)

    saved = {
        "source": "Open-Meteo CAMS Global",
        "metric": "modeled US AQI",
        "location": "Kuala Lumpur",
        "sample_time_local": sample_time.isoformat(),
        "us_aqi": round(value),
        "retrieved_at": datetime.now(TIMEZONE).isoformat(),
        "url": url,
    }
    try:
        cache.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return saved
