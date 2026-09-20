"""Solar phase context for video overlays."""

from __future__ import annotations

from datetime import datetime, timedelta

from astral import SunDirection
from astral.sun import blue_hour, golden_hour

from post_metadata import OBSERVER, TIMEZONE


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
