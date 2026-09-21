"""Small date-driven solar diagrams for the Director overlay.

The analemma uses NOAA's equation-of-time and declination approximations at
local noon. Horizon azimuth assumes a level horizon and standard refraction.
"""

from __future__ import annotations

import math
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

LATITUDE = 3.1390


def solar_terms(day: date) -> tuple[float, float]:
    """Return equation of time (minutes) and declination (radians)."""
    days = 366 if date(day.year, 12, 31).timetuple().tm_yday == 366 else 365
    gamma = 2 * math.pi / days * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma)
                       - 0.032077 * math.sin(gamma) - 0.014615 * math.cos(2 * gamma)
                       - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
            - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
            - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    return eqtime, decl


def annual_points(year: int) -> list[tuple[date, float, float]]:
    day = date(year, 1, 1)
    points = []
    while day.year == year:
        eqtime, decl = solar_terms(day)
        points.append((day, eqtime, decl))
        day += timedelta(days=1)
    return points


def approximate_season_dates(year: int) -> dict[str, date]:
    points = annual_points(year)
    def closest(months, key):
        return min((p for p in points if p[0].month in months), key=key)[0]
    return {
        "March equinox": closest((3, 4), lambda p: abs(p[2])) - timedelta(days=1),
        "June solstice": closest((6, 7), lambda p: -p[2]) - timedelta(days=1),
        "September equinox": closest((9, 10), lambda p: abs(p[2])) - timedelta(days=1),
        "December solstice": closest((12,), lambda p: p[2]) - timedelta(days=1),
    }


def season_dates(year: int, cache_dir: Path | None = None) -> dict[str, date]:
    """Use USNO UTC event instants, converted to Malaysia local dates."""
    cache = cache_dir / f"solar_seasons_{year}.json" if cache_dir else None
    try:
        if cache and cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
        else:
            with urlopen(f"https://aa.usno.navy.mil/api/seasons?year={year}", timeout=6) as response:
                payload = json.load(response)
            if cache:
                cache.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        result = {}
        for event in payload["data"]:
            name = event.get("phenom", "").lower()
            month = int(event["month"])
            if name not in ("equinox", "solstice"):
                continue
            utc = datetime.strptime(
                f"{event['year']}-{month}-{event['day']} {event['time']}",
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=timezone.utc)
            local_day = (utc + timedelta(hours=8)).date()
            key = ("March" if month == 3 else "June" if month == 6 else
                   "September" if month == 9 else "December") + f" {name}"
            result[key] = local_day
        if len(result) == 4:
            return result
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if year == 2026:
        # Verified USNO event instants converted from UTC to Kuala Lumpur time.
        return {
            "March equinox": date(2026, 3, 20),
            "June solstice": date(2026, 6, 21),
            "September equinox": date(2026, 9, 23),
            "December solstice": date(2026, 12, 22),
        }
    return approximate_season_dates(year)


def horizon_azimuth(day: date, direction: str, latitude: float = LATITUDE) -> float:
    """Azimuth clockwise from north, at an apparent horizon of -0.833 degrees."""
    _, decl = solar_terms(day)
    lat = math.radians(latitude)
    alt = math.radians(-0.833)
    cosine = (math.sin(decl) - math.sin(lat) * math.sin(alt)) / (math.cos(lat) * math.cos(alt))
    east_azimuth = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    if direction == "sunrise":
        return east_azimuth
    if direction == "sunset":
        return 360.0 - east_azimuth
    raise ValueError(f"Unsupported horizon direction: {direction}")


def run_direction(rows: list[dict]) -> str | None:
    from datetime import datetime
    hour = datetime.fromisoformat(rows[0]["time"]).hour
    if 4 <= hour < 11:
        return "sunrise"
    if 15 <= hour < 23:
        return "sunset"
    return None


def draw_solar_diagrams(draw, width: int, height: int, day: date, direction: str | None,
                        seasons: dict[str, date], text, font):
    """Draw the horizon scale and analemma in the Director's upper-right space."""
    white = (255, 255, 255, 245)
    black = (0, 0, 0, 230)
    thin = max(2, round(width * 0.002))
    small = font(0.011)
    label = font(0.013)

    if direction in ("sunrise", "sunset"):
        left, right, cy = width * 0.36, width * 0.69, height * 0.227
        center = (left + right) / 2
        draw.line((left, cy, right, cy), fill=black, width=thin + 3)
        draw.line((left, cy, right, cy), fill=white, width=thin)
        draw.line((center, cy - height * 0.010, center, cy + height * 0.006), fill=black, width=thin + 3)
        draw.line((center, cy - height * 0.010, center, cy + height * 0.006), fill=white, width=thin)
        text((center, cy - height * 0.014), "W" if direction == "sunset" else "E", label, anchor="ms")
        azimuth = horizon_azimuth(day, direction)
        due = 270 if direction == "sunset" else 90
        # The ends cover roughly the annual azimuth range at Kuala Lumpur.
        marker_x = max(left, min(right, center + (azimuth - due) / 27 * (right - left) / 2))
        r = width * 0.006
        draw.ellipse((marker_x-r, cy-r, marker_x+r, cy+r), fill=white, outline=black, width=2)
        text((center, cy + height * 0.016),
             f"{direction.upper()} {azimuth:.1f}\N{DEGREE SIGN}", small, anchor="ms")

    points = annual_points(day.year)
    curve = []
    for _, eqtime, decl in points:
        curve.append((width * 0.835 + width * 0.0035 * eqtime,
                      height * 0.170 - height * 0.0019 * math.degrees(decl)))
    draw.line(curve + [curve[0]], fill=black, width=thin + 3, joint="curve")
    draw.line(curve + [curve[0]], fill=white, width=thin, joint="curve")
    eqtime, decl = solar_terms(day)
    px = width * 0.835 + width * 0.0035 * eqtime
    py = height * 0.170 - height * 0.0019 * math.degrees(decl)
    r = width * 0.007
    draw.ellipse((px-r, py-r, px+r, py+r), fill=white, outline=black, width=2)
    text((width * 0.835, height * 0.095), seasons["June solstice"].strftime("%d %b"), small, anchor="ms")
    text((width * 0.835, height * 0.236), seasons["December solstice"].strftime("%d %b"), small, anchor="ms")
    for name, event_day in seasons.items():
        if day == event_day:
            text((width * 0.865, height * 0.172), name.split()[1].upper(), small, anchor="ls")
            break
