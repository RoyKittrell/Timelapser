#!/usr/bin/env python3
"""Expected Aperture Priority exposure and safe wired-trigger cadence.

Legacy curves provide a fallback until a settings-epoch calibration exists.
Current curves are learned from full-resolution JPEG EXIF, relative to the
scheduled solar event, with interpolation in exposure-value space.
"""

from __future__ import annotations

import math
import json
import os
from dataclasses import dataclass
from pathlib import Path


MODEL_SOURCE = {
    "sunrise": "20261001_062019_sunrise_v5_aperture_priority_wifi",
    "sunset": "20260930_182245_sunset_v5_aperture_priority_wifi",
}

DEFAULT_CALIBRATION_PATH = Path.home() / ".local" / "share" / "timelapser" / "v6_exposure_calibration.json"

EXPECTED_ISO = 200
EXPECTED_APERTURE = 5.6
DEFAULT_START_OFFSET_MINUTES = {"sunrise": -40.0, "sunset": -45.0}

# (minutes from solar event, actual shutter seconds)
EXPOSURE_CURVES = {
    "sunrise": (
        (-40, 6.0), (-34, 6.0), (-29, 5.0), (-24, 3.2), (-19, 2.0),
        (-14, 0.625), (-9, 1 / 6), (-4, 1 / 13), (1, 1 / 25),
        (6, 1 / 50), (11, 1 / 80), (16, 1 / 100), (21, 1 / 125),
        (26, 1 / 160), (31, 1 / 200), (36, 1 / 250), (41, 1 / 320),
        (50, 1 / 320),
    ),
    "sunset": (
        (-45, 1 / 60), (-37, 1 / 50), (-32, 1 / 60), (-27, 1 / 50),
        (-22, 1 / 50), (-17, 1 / 40), (-12, 1 / 25), (-7, 1 / 15),
        (-2, 1 / 8), (3, 1 / 4), (8, 0.625), (13, 1.6),
        (18, 2.5), (23, 3.2), (70, 3.2),
    ),
}


@dataclass(frozen=True)
class CadencePrediction:
    mode: str
    event_offset_minutes: float
    shutter_seconds: float
    busy_seconds: float
    interval_seconds: float
    expected_iso: int = EXPECTED_ISO
    expected_aperture: float = EXPECTED_APERTURE


def _interpolate(points: tuple[tuple[float, float], ...], event_offset_minutes: float) -> float:
    if event_offset_minutes <= points[0][0]:
        return points[0][1]
    if event_offset_minutes >= points[-1][0]:
        return points[-1][1]

    for (left_minute, left_shutter), (right_minute, right_shutter) in zip(points, points[1:]):
        if left_minute <= event_offset_minutes <= right_minute:
            fraction = (event_offset_minutes - left_minute) / (right_minute - left_minute)
            left_log = math.log2(left_shutter)
            right_log = math.log2(right_shutter)
            return 2 ** (left_log + fraction * (right_log - left_log))
    raise AssertionError("Exposure curve interpolation failed")


def calibration_path() -> Path:
    configured = os.environ.get("TIMELAPSER_V6_EXPOSURE_CALIBRATION")
    return Path(configured).expanduser() if configured else DEFAULT_CALIBRATION_PATH


def learned_curve(mode: str) -> tuple[tuple[float, float], ...] | None:
    try:
        payload = json.loads(calibration_path().read_text(encoding="utf-8"))
        raw_points = payload.get("modes", {}).get(mode, {}).get("points", [])
        points = tuple((float(minute), float(shutter)) for minute, shutter in raw_points)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return points if len(points) >= 2 and all(shutter > 0 for _, shutter in points) else None


def learned_curve_replaces_baseline() -> bool:
    try:
        payload = json.loads(calibration_path().read_text(encoding="utf-8"))
        return int(payload.get("version", 0)) >= 2 and bool(payload.get("settings_epoch"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def model_source(mode: str) -> str | None:
    learned = learned_curve(mode)
    if learned:
        relationship = (
            "new-settings learned calibration"
            if learned_curve_replaces_baseline()
            else "baseline plus learned calibration"
        )
        return f"{relationship}: {calibration_path()}"
    return MODEL_SOURCE.get(mode)


def expected_shutter_seconds(mode: str, event_offset_minutes: float) -> float | None:
    points = EXPOSURE_CURVES.get(mode)
    if not points:
        return None
    baseline = _interpolate(points, event_offset_minutes)
    learned = learned_curve(mode)
    if not learned:
        return baseline
    if learned_curve_replaces_baseline():
        return _interpolate(learned, event_offset_minutes)
    return max(baseline, _interpolate(learned, event_offset_minutes))


def expected_busy_seconds(shutter_seconds: float) -> float:
    # At four seconds and above, allow for an exposure-sized dark-frame/noise-
    # reduction cycle. The former extra five-second penalty was learned from
    # one old-settings failure and was unnecessarily throttling current runs.
    if shutter_seconds >= 4.0:
        return 2 * shutter_seconds
    return shutter_seconds + 2.5


def cadence_prediction(mode: str, event_offset_minutes: float, base_interval: float) -> CadencePrediction | None:
    shutter = expected_shutter_seconds(mode, event_offset_minutes)
    if shutter is None:
        return None
    busy = expected_busy_seconds(shutter)
    # Short exposures retain explicit write/relay headroom. Long exposures
    # already reserve a full second exposure for dark-frame processing, so do
    # not add another delay beyond that conservative allowance.
    headroom = 0.0 if shutter >= 4.0 else 0.5
    safe_interval = math.ceil((busy + headroom) * 2) / 2
    return CadencePrediction(
        mode=mode,
        event_offset_minutes=event_offset_minutes,
        shutter_seconds=shutter,
        busy_seconds=busy,
        interval_seconds=max(base_interval, safe_interval),
    )
