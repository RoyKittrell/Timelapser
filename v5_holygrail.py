import math
from dataclasses import dataclass
from typing import Optional

from v5_config import (
    AI_EXPOSURE_DEADBAND_EV,
    HOLY_GRAIL_ANOMALY_THRESHOLD_EV,
    HOLY_GRAIL_RECENCY_DECAY,
    HOLY_GRAIL_TARGET_P50_DAY,
    HOLY_GRAIL_TARGET_P50_NIGHT,
    HOLY_GRAIL_TRACKER_WARMUP,
    HOLY_GRAIL_TRACKER_WINDOW,
    MAX_AI_EXPOSURE_STEP_EV,
    MAX_ISO,
    MAX_SHUTTER_SECONDS,
    MIN_ISO,
    MIN_SHUTTER_SECONDS,
    PREFERRED_MAX_SHUTTER_SECONDS,
)


@dataclass
class ExposureSettings:
    iso: int
    aperture: float
    shutter_seconds: float


@dataclass
class MeterSample:
    frame: int
    monotonic: float
    scene_ev: float
    raw_pixel_ev: float
    median: float
    highlights_pct: float
    shadows_pct: float
    weight: float = 1.0
    is_anomaly: bool = False


def exposure_ev(settings: ExposureSettings) -> float:
    return math.log2(
        max(
            1e-12,
            settings.shutter_seconds * settings.iso / (settings.aperture * settings.aperture),
        )
    )


def p50_to_pixel_ev(p50_luma: float) -> float:
    lum = max(float(p50_luma), 1.0) / 255.0
    return math.log2(max(lum ** 2.2, 1e-9) / 0.18) + 12.0


def _weighted_slope(xs, ys, weights) -> float:
    if len(xs) < 2:
        return 0.0
    w_sum = sum(weights)
    if w_sum <= 0:
        return 0.0
    x_bar = sum(x * w for x, w in zip(xs, weights)) / w_sum
    y_bar = sum(y * w for y, w in zip(ys, weights)) / w_sum
    num = sum(w * (x - x_bar) * (y - y_bar) for x, y, w in zip(xs, ys, weights))
    den = sum(w * (x - x_bar) ** 2 for x, w in zip(xs, weights))
    return num / den if abs(den) > 1e-10 else 0.0


class ShadowHolyGrailController:
    """Exposure-normalized metering model used for logging only.

    Each thumbnail's P50 brightness is converted to pixel EV and normalized by
    the camera exposure used for that frame. The result estimates scene EV, so
    future logic can reason about real light changes instead of chasing its own
    shutter/ISO changes.
    """

    def __init__(self):
        self.samples: list[MeterSample] = []
        self.last_recommendation: dict = {}

    @property
    def warm(self) -> bool:
        return len(self.samples) >= int(HOLY_GRAIL_TRACKER_WARMUP)

    def push_frame(self, frame_no: int, monotonic: float, metrics: dict, settings: ExposureSettings) -> dict:
        raw_pixel_ev = metrics.get("pixel_ev_p50")
        if raw_pixel_ev is None:
            p50_luma = float(metrics.get("p50_luma", metrics.get("median", 0.0) * 255.0))
            raw_pixel_ev = p50_to_pixel_ev(p50_luma)

        scene_ev = float(raw_pixel_ev) - exposure_ev(settings)
        sample = MeterSample(
            frame=int(frame_no),
            monotonic=float(monotonic),
            scene_ev=scene_ev,
            raw_pixel_ev=float(raw_pixel_ev),
            median=float(metrics.get("median", 0.0) or 0.0),
            highlights_pct=float(metrics.get("highlights_pct", 0.0) or 0.0),
            shadows_pct=float(metrics.get("shadows_pct", 0.0) or 0.0),
        )

        if len(self.samples) >= 3:
            recent = sorted(s.scene_ev for s in self.samples[-10:])
            median_ev = recent[len(recent) // 2]
            if abs(sample.scene_ev - median_ev) > float(HOLY_GRAIL_ANOMALY_THRESHOLD_EV):
                sample.is_anomaly = True
                sample.weight = 0.15
                if self.samples[-1].is_anomaly and (
                    sample.scene_ev - median_ev
                ) * (self.samples[-1].scene_ev - median_ev) > 0:
                    sample.weight = 1.0

        self.samples.append(sample)
        self.samples = self.samples[-int(HOLY_GRAIL_TRACKER_WINDOW):]
        return self.status()

    def status(self) -> dict:
        slope = self._scene_slope_ev_per_second()
        latest = self.samples[-1] if self.samples else None
        return {
            "samples": len(self.samples),
            "warm": self.warm,
            "scene_ev": round(latest.scene_ev, 4) if latest else None,
            "raw_pixel_ev": round(latest.raw_pixel_ev, 4) if latest else None,
            "scene_slope_ev_per_second": round(slope, 6),
            "scene_slope_ev_per_minute": round(slope * 60.0, 4),
            "latest_is_anomaly": bool(latest.is_anomaly) if latest else False,
        }

    def recommend(self, mode: str, current: ExposureSettings) -> dict:
        if not self.samples:
            return {"available": False, "reason": "no_meter_samples"}

        latest = self.samples[-1]
        target_p50 = (
            HOLY_GRAIL_TARGET_P50_NIGHT
            if str(mode).lower() == "sunrise" and current.shutter_seconds >= 0.5
            else HOLY_GRAIL_TARGET_P50_DAY
        )
        target_pixel_ev = p50_to_pixel_ev(target_p50)
        current_ev = exposure_ev(current)
        target_exposure_ev = target_pixel_ev - latest.scene_ev
        delta_ev = target_exposure_ev - current_ev

        slope = self._scene_slope_ev_per_second()
        if self.warm:
            # Pre-bias one frame using the measured scene trend.
            delta_ev -= slope * 30.0

        clamped_delta = max(
            -float(MAX_AI_EXPOSURE_STEP_EV),
            min(float(MAX_AI_EXPOSURE_STEP_EV), delta_ev),
        )
        if abs(clamped_delta) < float(AI_EXPOSURE_DEADBAND_EV):
            action = "HOLD"
            clamped_delta = 0.0
        elif clamped_delta > 0:
            action = "BRIGHTEN"
        else:
            action = "DARKEN"

        proposed = settings_for_delta_ev(current, clamped_delta)
        self.last_recommendation = {
            "available": True,
            "action": action,
            "reason": "shadow_only",
            "target_p50": int(target_p50),
            "target_delta_ev": round(delta_ev, 4),
            "recommended_delta_ev": round(clamped_delta, 4),
            "current": current.__dict__,
            "proposed": proposed.__dict__,
            **self.status(),
        }
        return dict(self.last_recommendation)

    def _scene_slope_ev_per_second(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        first_t = self.samples[0].monotonic
        xs = [s.monotonic - first_t for s in self.samples]
        ys = [s.scene_ev for s in self.samples]
        n = len(self.samples)
        recency = [float(HOLY_GRAIL_RECENCY_DECAY) ** i for i in range(n - 1, -1, -1)]
        weights = [s.weight * r for s, r in zip(self.samples, recency)]
        return _weighted_slope(xs, ys, weights)


def settings_for_delta_ev(current: ExposureSettings, delta_ev: float) -> ExposureSettings:
    if delta_ev == 0:
        return ExposureSettings(current.iso, current.aperture, current.shutter_seconds)

    target_ev = exposure_ev(current) + float(delta_ev)
    aperture = float(current.aperture)
    iso = int(current.iso)

    def shutter_for(target_iso: int) -> float:
        return (2.0 ** target_ev) * (aperture * aperture) / float(target_iso)

    if delta_ev > 0:
        shutter = max(float(current.shutter_seconds), min(MAX_SHUTTER_SECONDS, shutter_for(iso)))
        if shutter > PREFERRED_MAX_SHUTTER_SECONDS and iso < MAX_ISO:
            iso = min(MAX_ISO, max(MIN_ISO, int(round(iso * shutter / PREFERRED_MAX_SHUTTER_SECONDS))))
            shutter = shutter_for(iso)
        shutter = max(MIN_SHUTTER_SECONDS, min(MAX_SHUTTER_SECONDS, shutter))
        return ExposureSettings(iso=iso, aperture=aperture, shutter_seconds=shutter)

    shutter = max(MIN_SHUTTER_SECONDS, min(float(current.shutter_seconds), shutter_for(iso)))
    if shutter <= MIN_SHUTTER_SECONDS and iso > MIN_ISO:
        iso = max(MIN_ISO, int(round((2.0 ** target_ev) * (aperture * aperture) / shutter)))
    return ExposureSettings(iso=iso, aperture=aperture, shutter_seconds=shutter)
