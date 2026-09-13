#!/usr/bin/env python3
"""
Timelapser V5.0.9 — Autonomous Empty-Card Bootstrap

Olympus E-M1 Mark III + Raspberry Pi + dedicated TP-Link Wi-Fi adapter.

Architecture:
    wlan0 -> normal LAN / internet / SSH / Tailscale
    wlan1 -> E-M1 III direct Wi-Fi network

    E-M1 III JPEG (recommended; RAW+JPEG still tolerated)
        -> deterministic Python camera executor via olympuswifi
        -> small camera thumbnail downloaded to Pi
        -> local OpenCV image metrics
        -> asynchronous AI Commander
        -> governed exposure intentions
        -> next physical still

Full-size stills remain on the camera SD card in thumbnail mode. V5.0.6 indexes
the SD card once at startup, predicts each next Olympus JPEG filename, and requests
that thumbnail directly. A full list_images() call is used only if prediction fails.
The successful thumbnail probe is cached, so analysis does not download it twice.
"""

import argparse
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from collections import deque

from v5_config import *
from v5_logging import RunLogger
from v5_state import TimelapseState, SharedState
from v5_camera import WifiCameraController
from v5_commander import AICommander
from v5_filesystem import ensure_run_tree
from v5_image import analyse_jpeg
from v5_holygrail import ExposureSettings, ShadowHolyGrailController
from timelapser_postprocess_v5 import run_postprocess


def load_env_file(path: Path):
    path = Path(path)
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


def clear_stale_stop_request(logger):
    if not STOP_REQUEST_FILE.exists():
        return
    try:
        STOP_REQUEST_FILE.unlink()
        logger.event("operator_stop_request_cleared_at_startup", path=str(STOP_REQUEST_FILE))
    except Exception as exc:
        logger.error("clear_stale_stop_request", exc, path=str(STOP_REQUEST_FILE))


def consume_stop_request(logger):
    if not STOP_REQUEST_FILE.exists():
        return None
    payload = {}
    try:
        payload = json.loads(STOP_REQUEST_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("read_stop_request", exc, path=str(STOP_REQUEST_FILE))
    try:
        STOP_REQUEST_FILE.unlink()
    except Exception as exc:
        logger.error("unlink_stop_request", exc, path=str(STOP_REQUEST_FILE))
    reason = str(payload.get("reason") or "operator requested graceful stop")
    return {"reason": reason, "payload": payload}


def apply_operator_stop_if_requested(state, logger):
    stop = consume_stop_request(logger)
    if stop is None:
        return False
    reason = stop["reason"]
    logger.human(f"Operator requested graceful stop: {reason}")
    logger.event("operator_stop_requested", **stop["payload"])
    state.update(lambda s: (
        setattr(s, "abort_requested", True),
        setattr(s, "abort_reason", reason),
    ))
    return True


def apply_camera_result_to_state(state, result):
    applied = result.get("applied", result)

    def update(s):
        if "iso" in applied:
            s.iso = int(applied["iso"])
        if "aperture" in applied:
            s.aperture = float(applied["aperture"])
        if "shutter_seconds" in applied:
            s.shutter_seconds = float(applied["shutter_seconds"])

    state.update(update)


def apply_baseline(camera, state, mode, logger, override=None):
    baselines = {
        "sunset": SUNSET_BASELINE,
        "sunrise": SUNRISE_BASELINE,
        "general": GENERAL_BASELINE,
    }
    baseline = dict(baselines[mode])
    if override:
        baseline.update({k: v for k, v in override.items() if v is not None})
        logger.human(
            "Scheduler/CLI baseline override: "
            f"ISO {baseline['iso']} | f/{baseline['aperture']} | "
            f"{baseline['shutter_seconds']:.6g}s"
        )
    outcome = camera.set_exposure(**baseline)
    if not outcome["ok"]:
        raise RuntimeError("Could not establish baseline exposure over Olympus Wi-Fi")
    apply_camera_result_to_state(state, outcome)
    if outcome.get("failed_kinds"):
        logger.human(
            "Baseline partially applied; failed: " + ", ".join(outcome["failed_kinds"])
        )


def exposure_ev(iso, aperture, shutter):
    # Exposure proportional to shutter * ISO / f^2.
    return math.log2(max(1e-12, shutter * iso / (aperture * aperture)))



def summarise_scene_trend(history):
    """Summarise measured brightness evolution for AI + governor.

    history items are dicts with monotonic, frame, median, highlights_pct,
    shadows_pct.  We intentionally use measured frame statistics rather than
    assuming a sunrise/sunset direction from the run label.
    """
    if not history:
        return {
            "samples": 0,
            "classification": "unknown",
            "delta_1m": None,
            "delta_5m": None,
            "median_now": None,
        }

    latest = history[-1]
    now_t = latest["monotonic"]
    now_m = float(latest["median"])

    def value_ago(seconds):
        target = now_t - seconds
        candidates = [x for x in history if x["monotonic"] <= target]
        if not candidates:
            return None
        return float(candidates[-1]["median"])

    m1 = value_ago(60.0)
    m5 = value_ago(300.0)
    d1 = None if m1 is None else now_m - m1
    d5 = None if m5 is None else now_m - m5

    # Classification deliberately requires sustained evidence.
    if d5 is not None:
        if d5 <= -0.050:
            cls = "strong_dimming"
        elif d5 <= -0.020:
            cls = "dimming"
        elif d5 >= 0.050:
            cls = "strong_brightening"
        elif d5 >= 0.020:
            cls = "brightening"
        else:
            cls = "stable"
    elif d1 is not None:
        if d1 <= -0.020:
            cls = "dimming"
        elif d1 >= 0.020:
            cls = "brightening"
        else:
            cls = "stable"
    else:
        cls = "insufficient_history"

    return {
        "samples": len(history),
        "frame_now": int(latest["frame"]),
        "median_now": now_m,
        "highlights_pct_now": float(latest.get("highlights_pct", 0.0) or 0.0),
        "shadows_pct_now": float(latest.get("shadows_pct", 0.0) or 0.0),
        "median_1m_ago": m1,
        "median_5m_ago": m5,
        "delta_1m": d1,
        "delta_5m": d5,
        "classification": cls,
    }


def govern_periodic_exposure(action, snap, logger):
    """Trend-aware deterministic guardrail around periodic AI exposure changes.

    V5.0.4 proved the LLM can anchor on the word "sunset" and repeatedly request
    brightening even while measured daylight is stable. V5.0.6 therefore treats
    the measured temporal trend as authoritative.
    """
    current = {
        "iso": int(snap["iso"]),
        "aperture": float(snap["aperture"]),
        "shutter_seconds": float(snap["shutter_seconds"]),
    }
    requested = {
        "iso": max(MIN_ISO, min(MAX_ISO, int(action.iso if action.iso is not None else current["iso"]))),
        "aperture": max(
            MIN_APERTURE,
            min(MAX_APERTURE, float(action.aperture if action.aperture is not None else current["aperture"])),
        ),
        "shutter_seconds": max(
            MIN_SHUTTER_SECONDS,
            min(
                MAX_SHUTTER_SECONDS,
                float(action.shutter_seconds if action.shutter_seconds is not None else current["shutter_seconds"]),
            ),
        ),
    }

    cur_ev = exposure_ev(current["iso"], current["aperture"], current["shutter_seconds"])
    req_ev = exposure_ev(requested["iso"], requested["aperture"], requested["shutter_seconds"])
    delta = req_ev - cur_ev

    metrics = snap.get("last_image_metrics") or {}
    median = metrics.get("median")
    highlights = float(metrics.get("highlights_pct", 0.0) or 0.0)
    shadows = float(metrics.get("shadows_pct", 0.0) or 0.0)
    trend = snap.get("scene_trend") or {}
    trend_class = str(trend.get("classification", "unknown"))
    d1 = trend.get("delta_1m")
    d5 = trend.get("delta_5m")

    deadband = float(AI_EXPOSURE_DEADBAND_EV)
    if abs(delta) < deadband:
        logger.event(
            "ai_exposure_rejected",
            reason="deadband",
            delta_ev=delta,
            deadband_ev=deadband,
            median=median,
            highlights_pct=highlights,
            shadows_pct=shadows,
            scene_trend=trend,
            requested=action.__dict__,
        )
        return None

    last_delta = snap.get("ai_last_applied_delta_ev")
    reversal_deadband = float(AI_EXPOSURE_REVERSAL_DEADBAND_EV)
    if (
        last_delta is not None
        and float(last_delta) != 0.0
        and math.copysign(1.0, float(last_delta)) != math.copysign(1.0, delta)
        and abs(delta) < reversal_deadband
    ):
        logger.human(
            f"Exposure governor: ignored {delta:+.2f} EV reversal; last applied "
            f"change was {float(last_delta):+.2f} EV."
        )
        logger.event(
            "ai_exposure_rejected",
            reason="reversal_deadband",
            delta_ev=delta,
            last_applied_delta_ev=float(last_delta),
            reversal_deadband_ev=reversal_deadband,
            median=median,
            highlights_pct=highlights,
            shadows_pct=shadows,
            scene_trend=trend,
            requested=action.__dict__,
        )
        return None

    # HARD highlight protection.
    if delta > 0 and highlights >= 2.0:
        logger.human(
            f"Exposure governor: rejected +{delta:.2f} EV; highlights already "
            f"{highlights:.2f}%."
        )
        logger.event(
            "ai_exposure_rejected",
            reason="highlight_protection",
            delta_ev=delta,
            median=median,
            highlights_pct=highlights,
            shadows_pct=shadows,
            scene_trend=trend,
            requested=action.__dict__,
        )
        return None

    # Brightening requires either an actually dark frame or measured fading.
    if delta > 0 and median is not None:
        dark_now = float(median) < 0.46
        measured_fading = (
            trend_class in {"dimming", "strong_dimming"}
            or (d1 is not None and float(d1) <= -0.015)
            or (d5 is not None and float(d5) <= -0.020)
        )
        if not dark_now and not measured_fading:
            logger.human(
                f"Exposure governor: rejected +{delta:.2f} EV; scene is "
                f"{trend_class} (median={float(median):.3f}, "
                f"d1m={d1 if d1 is not None else 'n/a'}, "
                f"d5m={d5 if d5 is not None else 'n/a'})."
            )
            logger.event(
                "ai_exposure_rejected",
                reason="no_measured_need_to_brighten",
                delta_ev=delta,
                median=float(median),
                highlights_pct=highlights,
                shadows_pct=shadows,
                scene_trend=trend,
                requested=action.__dict__,
            )
            return None

    # Darkening requires clipping/high brightness or measured brightening.
    if delta < 0 and median is not None:
        bright_now = float(median) > 0.70 or highlights >= 1.0
        measured_brightening = (
            trend_class in {"brightening", "strong_brightening"}
            or (d1 is not None and float(d1) >= 0.015)
            or (d5 is not None and float(d5) >= 0.020)
        )
        if not bright_now and not measured_brightening:
            logger.human(
                f"Exposure governor: rejected {delta:.2f} EV; no measured need "
                f"to darken (median={float(median):.3f}, trend={trend_class})."
            )
            logger.event(
                "ai_exposure_rejected",
                reason="no_measured_need_to_darken",
                delta_ev=delta,
                median=float(median),
                highlights_pct=highlights,
                shadows_pct=shadows,
                scene_trend=trend,
                requested=action.__dict__,
            )
            return None

    def shutter_for_ev(iso, aperture, ev):
        return (2 ** ev) * (float(aperture) ** 2) / float(iso)

    def clamp_shutter(value):
        return max(MIN_SHUTTER_SECONDS, min(MAX_SHUTTER_SECONDS, float(value)))

    def prefer_iso_over_long_shutter(settings):
        if (
            settings["shutter_seconds"] <= PREFERRED_MAX_SHUTTER_SECONDS
            or settings["iso"] >= MAX_ISO
        ):
            return settings
        original = dict(settings)
        target_iso = min(
            MAX_ISO,
            int(round(
                settings["iso"]
                * settings["shutter_seconds"]
                / PREFERRED_MAX_SHUTTER_SECONDS
            )),
        )
        if target_iso <= settings["iso"]:
            return settings
        settings = dict(settings)
        settings["shutter_seconds"] = clamp_shutter(
            settings["shutter_seconds"] * settings["iso"] / target_iso
        )
        settings["iso"] = target_iso
        logger.event(
            "ai_exposure_shutter_iso_preference_applied",
            preferred_max_shutter_seconds=float(PREFERRED_MAX_SHUTTER_SECONDS),
            original=original,
            governed=settings,
            requested=action.__dict__,
        )
        return settings

    def settings_for_clamped_ev(target_ev, direction):
        # Periodic timelapse adjustments should be visually smooth, so keep
        # aperture stable and solve the requested EV with shutter/ISO only.
        governed = dict(current)
        governed["aperture"] = current["aperture"]

        if direction > 0:
            target_shutter = clamp_shutter(
                shutter_for_ev(governed["iso"], governed["aperture"], target_ev)
            )
            if (
                target_shutter <= PREFERRED_MAX_SHUTTER_SECONDS
                or governed["iso"] >= MAX_ISO
            ):
                governed["shutter_seconds"] = target_shutter
                return governed

            governed["shutter_seconds"] = min(
                MAX_SHUTTER_SECONDS,
                max(current["shutter_seconds"], PREFERRED_MAX_SHUTTER_SECONDS),
            )
            target_iso = int(round(
                (2 ** target_ev)
                * (governed["aperture"] ** 2)
                / governed["shutter_seconds"]
            ))
            governed["iso"] = max(MIN_ISO, min(MAX_ISO, target_iso))
            return governed

        governed["shutter_seconds"] = clamp_shutter(
            shutter_for_ev(governed["iso"], governed["aperture"], target_ev)
        )
        if governed["shutter_seconds"] <= MIN_SHUTTER_SECONDS and governed["iso"] > MIN_ISO:
            target_iso = int(round(
                (2 ** target_ev)
                * (governed["aperture"] ** 2)
                / governed["shutter_seconds"]
            ))
            governed["iso"] = max(MIN_ISO, min(governed["iso"], target_iso))
        return governed

    # Clamp the total jump to the configured maximum while preserving the AI
    # direction.  Periodic changes deliberately keep aperture stable; startup is
    # where the AI gets to choose the baseline aperture.
    if abs(delta) > MAX_AI_EXPOSURE_STEP_EV:
        clamped_delta = math.copysign(MAX_AI_EXPOSURE_STEP_EV, delta)
        target_ev = cur_ev + clamped_delta
        requested = settings_for_clamped_ev(target_ev, delta)
        delta = exposure_ev(
            requested["iso"],
            requested["aperture"],
            requested["shutter_seconds"],
        ) - cur_ev
        logger.human(
            f"Exposure governor: clamped AI request to {delta:+.2f} EV."
        )
    elif delta > 0:
        requested = prefer_iso_over_long_shutter(requested)
        delta = exposure_ev(
            requested["iso"],
            requested["aperture"],
            requested["shutter_seconds"],
        ) - cur_ev

    logger.event(
        "ai_exposure_governor_approved",
        delta_ev=delta,
        current=current,
        governed=requested,
        median=median,
        highlights_pct=highlights,
        shadows_pct=shadows,
        scene_trend=trend,
        requested=action.__dict__,
    )
    return requested

def execute_ai_actions(camera, commander, state, logger):
    """Drain ready AI actions without ever allowing exposure work to starve.

    V5.0.7 only called this function while there was idle time before the next
    scheduled frame. With a 4 s exposure plus mode switches/thumbnail analysis,
    a 5 s cadence can have no idle window at all. That caused the commander
    result queue to fill permanently while the camera remained at stale settings.

    V5.0.8 still collapses a burst to the newest SET_EXPOSURE, but the main loop
    now calls us before every physical capture as well as during idle time.
    """
    actions = commander.get_actions_nowait()
    if not actions:
        return False

    latest_set = None
    non_sets = []
    for action in actions:
        if action.action == "SET_EXPOSURE":
            latest_set = action
        else:
            non_sets.append(action)
    if latest_set is not None:
        non_sets.append(latest_set)

    did_work = False
    for action in non_sets:
        did_work = True
        logger.human(f"AI Commander action: {action.action} — {action.reason}")
        logger.event("ai_action_dequeued", action=action.__dict__)

        if action.action == "NOOP":
            snap = state.snapshot()
            median = (snap.get("last_image_metrics") or {}).get("median")
            if median is not None:
                state.update(lambda s: setattr(s, "ai_last_review_median", float(median)))
            continue

        if action.action == "ABORT":
            state.update(lambda s: (
                setattr(s, "abort_requested", True),
                setattr(s, "abort_reason", action.reason),
            ))
            return True

        if action.action == "WAIT":
            logger.event(
                "ai_wait_ignored_for_capture_cadence",
                requested_wait_seconds=float(action.wait_seconds or 0.0),
                reason=action.reason,
            )
            continue

        if action.action == "SET_EXPOSURE":
            snap = state.snapshot()
            governed = govern_periodic_exposure(action, snap, logger)
            median = (snap.get("last_image_metrics") or {}).get("median")
            if median is not None:
                state.update(lambda s: setattr(s, "ai_last_review_median", float(median)))
            if governed is None:
                continue

            logger.event(
                "ai_exposure_apply_requested",
                requested=action.__dict__,
                governed=governed,
            )
            outcome = camera.set_exposure(**governed)
            apply_camera_result_to_state(state, outcome)
            applied_snap = state.snapshot()
            applied_delta = exposure_ev(
                applied_snap["iso"],
                applied_snap["aperture"],
                applied_snap["shutter_seconds"],
            ) - exposure_ev(
                snap["iso"],
                snap["aperture"],
                snap["shutter_seconds"],
            )
            state.update(lambda s: (
                setattr(s, "ai_last_note", action.reason),
                setattr(s, "ai_last_decision_at", time.monotonic()),
                setattr(s, "ai_last_applied_delta_ev", applied_delta),
            ))
            logger.event(
                "ai_exposure_applied",
                requested=action.__dict__,
                governed=governed,
                outcome=outcome,
                applied_delta_ev=applied_delta,
            )
            if not outcome.get("ok", False):
                logger.human(
                    "WARNING: AI exposure request was not verified by camera read-back; "
                    f"failed={outcome.get('failed_kinds')} mismatched={outcome.get('mismatched_kinds')}"
                )

    return did_work


def apply_clipping_failsafe(camera, state, logger, scene_history):
    """Deterministic emergency darkening independent of the AI result queue.

    If three consecutive analysed frames are essentially saturated, reduce
    shutter by 2 EV. This is intentionally conservative about when it triggers,
    but decisive once triggered. It exists so a wedged/late AI path cannot leave
    an unattended sunrise at 100% clipping for tens of minutes.
    """
    if len(scene_history) < 3:
        return False
    recent = list(scene_history)[-3:]
    severe = all(
        float(x.get("median", 0.0) or 0.0) >= 0.97
        or float(x.get("highlights_pct", 0.0) or 0.0) >= 95.0
        for x in recent
    )
    if not severe:
        return False

    snap = state.snapshot()
    current_shutter = float(snap["shutter_seconds"])
    target_shutter = max(MIN_SHUTTER_SECONDS, current_shutter / 4.0)  # -2 EV
    if target_shutter >= current_shutter * 0.999:
        return False

    logger.human(
        "CLIPPING FAILSAFE: 3 consecutive near-saturated frames; "
        f"forcing shutter {current_shutter:.6g}s -> {target_shutter:.6g}s."
    )
    logger.event(
        "clipping_failsafe_requested",
        current={
            "iso": snap["iso"],
            "aperture": snap["aperture"],
            "shutter_seconds": current_shutter,
        },
        target_shutter_seconds=target_shutter,
        recent=recent,
    )
    outcome = camera.set_exposure(shutter_seconds=target_shutter)
    apply_camera_result_to_state(state, outcome)
    logger.event("clipping_failsafe_applied", outcome=outcome)
    return bool(outcome.get("ok", False))


def download_thumbnail(camera, remote_path, local_path, logger):
    """Download the camera's small thumbnail without fetching the full JPEG."""
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)

    wrapper = getattr(camera, "download_thumbnail", None)
    if callable(wrapper):
        result = wrapper(remote_path, local_path)
        if local_path.exists() and local_path.stat().st_size > 0:
            return local_path
        if isinstance(result, (bytes, bytearray)):
            local_path.write_bytes(result)
            return local_path

    for attr in ("cam", "camera", "_cam", "_camera", "client"):
        obj = getattr(camera, attr, None)
        method = getattr(obj, "download_thumbnail", None) if obj is not None else None
        if callable(method):
            data = method(remote_path)
            if not isinstance(data, (bytes, bytearray)):
                raise RuntimeError(
                    f"olympuswifi download_thumbnail returned {type(data).__name__}, expected bytes"
                )
            local_path.write_bytes(data)
            logger.event(
                "thumbnail_downloaded",
                remote_path=str(remote_path),
                local_path=str(local_path),
                bytes=len(data),
                controller_attr=attr,
            )
            return local_path

    raise RuntimeError(
        "Could not find olympuswifi download_thumbnail() through WifiCameraController. "
        "Inspect v5_camera.py and expose the wrapped OlympusCamera as cam/camera, "
        "or add a download_thumbnail wrapper."
    )


def capture_download_analyse(camera, paths, frame_no, logger, transfer_mode):
    """Capture one physical frame and fetch either a thumbnail or the full JPEG.

    HUMAN NOTE:
    - transfer_mode="thumbnail" is the safe/default choice for short cadences
      such as 5-30 seconds. The full-quality JPEG stays on the camera SD card,
      while the tiny Olympus thumbnail is used for OpenCV + AI analysis.
    - transfer_mode="full" is useful for long cadences (for example 1-2 minutes
      or more) when there is plenty of idle time between exposures and you want
      the Pi to accumulate the full-resolution JPEGs during the run.

    IMPORTANT: full Wi-Fi JPEG transfers have occasionally stalled for minutes
    on this camera. If a full transfer takes longer than the requested interval,
    it WILL delay the next frame. Use thumbnail mode whenever capture cadence is
    more important than having the full JPEG immediately on the Pi.
    """
    t0 = time.monotonic()
    result = camera.capture(frame_no)
    t1 = time.monotonic()

    local_jpg = paths["frames"] / f"frame_{frame_no:06d}.jpg"

    if transfer_mode == "full":
        camera.download_jpeg(result.remote_jpg, local_jpg)
        transfer_kind = "full"
    else:
        download_thumbnail(camera, result.remote_jpg, local_jpg, logger)
        transfer_kind = "thumbnail"

    t2 = time.monotonic()

    metrics = analyse_jpeg(local_jpg)
    t3 = time.monotonic()

    timings = {
        "camera_capture_seconds": t1 - t0,
        "transfer_seconds": t2 - t1,
        "transfer_mode": transfer_kind,
        # Preserve old columns so the current Director remains compatible.
        "thumbnail_download_seconds": (t2 - t1) if transfer_kind == "thumbnail" else 0.0,
        "jpeg_download_seconds": t2 - t1,
        "image_analysis_seconds": t3 - t2,
        "capture_cycle_seconds": t3 - t0,
    }
    return result, local_jpg, metrics, timings

def run_startup_scout(camera, commander, state, logger, paths, transfer_mode):
    logger.human("")
    logger.human(
        "Startup scout: taking one real still; "
        + ("downloading full JPEG..." if transfer_mode == "full" else "downloading thumbnail only...")
    )

    result = camera.capture(0)
    scout_jpg = paths["run"] / "startup_scout.jpg"
    if transfer_mode == "full":
        camera.download_jpeg(result.remote_jpg, scout_jpg)
    else:
        download_thumbnail(camera, result.remote_jpg, scout_jpg, logger)
    metrics = analyse_jpeg(scout_jpg)

    state.update(lambda s: (
        setattr(s, "last_remote_jpg", result.remote_jpg),
        setattr(s, "last_remote_orf", result.remote_orf),
        setattr(s, "last_local_jpeg", str(scout_jpg)),
        setattr(s, "last_preview_jpeg", str(scout_jpg)),
        setattr(s, "last_image_metrics", metrics),
    ))

    logger.human(
        "Startup scout metrics: "
        f"median={metrics.get('median', float('nan')):.3f} | "
        f"highlights={metrics.get('highlights_pct', float('nan')):.2f}% | "
        f"shadows={metrics.get('shadows_pct', float('nan')):.2f}%"
    )

    if not commander.enabled:
        logger.human("AI Commander disabled; keeping baseline exposure.")
        return

    logger.human(f"Startup scout: asking {AI_MODEL} for initial exposure...")
    actions = commander.startup_review(state.snapshot(), str(scout_jpg))
    exposure = next((a for a in actions if a.action == "SET_EXPOSURE"), None)
    if exposure is None:
        logger.human("WARNING: startup AI returned no valid exposure; keeping baseline.")
        logger.event("startup_ai_no_valid_exposure")
        return

    logger.human(
        f"Startup AI exposure: ISO {exposure.iso} | f/{exposure.aperture} | "
        f"{exposure.shutter_seconds:.6g}s"
    )
    logger.human(f"Startup AI reason: {exposure.reason}")

    outcome = camera.set_exposure(
        iso=exposure.iso,
        aperture=exposure.aperture,
        shutter_seconds=exposure.shutter_seconds,
    )
    apply_camera_result_to_state(state, outcome)
    state.update(lambda s: (
        setattr(s, "ai_last_note", exposure.reason),
        setattr(s, "ai_last_decision_at", time.monotonic()),
    ))
    logger.event("startup_ai_exposure_applied", outcome=outcome, reason=exposure.reason)


def refresh_observed_exposure(camera, state, logger):
    """Read the camera's current exposure without changing any camera setting."""
    snap = state.snapshot()
    observed = {
        "iso": int(snap["iso"]),
        "aperture": float(snap["aperture"]),
        "shutter_seconds": float(snap["shutter_seconds"]),
    }
    readback_errors = {}
    readers = {
        "iso": lambda: int(camera._read_exposure_value("iso")),
        "aperture": lambda: float(camera._read_exposure_value("aperture")),
        "shutter_seconds": lambda: float(camera._read_exposure_value("shutter")),
    }
    for key, reader in readers.items():
        try:
            observed[key] = reader()
        except Exception as exc:
            readback_errors[key] = f"{type(exc).__name__}: {exc}"

    state.update(lambda s: (
        setattr(s, "iso", observed["iso"]),
        setattr(s, "aperture", observed["aperture"]),
        setattr(s, "shutter_seconds", observed["shutter_seconds"]),
    ))
    if readback_errors:
        logger.event(
            "aperture_priority_exposure_observed_partial",
            observed=observed,
            readback_errors=readback_errors,
        )
    else:
        logger.event("aperture_priority_exposure_observed", **observed)
    return state.snapshot()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["sunset", "sunrise", "general"], default=MODE_DEFAULT)
    parser.add_argument("--interval", type=float, default=CAPTURE_INTERVAL_SECONDS)
    parser.add_argument("--max-frames", type=int, default=MAX_FRAMES)
    parser.add_argument(
        "--iso", type=int, default=None,
        help="Optional starting ISO override. If omitted, the mode baseline is used."
    )
    parser.add_argument(
        "--aperture", type=float, default=None,
        help="Optional starting aperture override (f-number). If omitted, the mode baseline is used."
    )
    parser.add_argument(
        "--shutter", type=float, default=None,
        help="Optional starting shutter duration in seconds. If omitted, the mode baseline is used."
    )
    parser.add_argument(
        "--max-run-minutes", type=float, default=MAX_RUN_MINUTES,
        help="Wall-clock safety ceiling for this run. Director uses this for explicit ad-hoc durations."
    )
    parser.add_argument(
        "--transfer-mode",
        choices=["thumbnail", "full"],
        default="thumbnail",
        help=(
            "Image transfer strategy. 'thumbnail' (default) is recommended for short "
            "intervals and keeps full JPEGs on the camera SD card. 'full' downloads "
            "each full-resolution JPEG during the run and is best suited to long "
            "intervals such as ~1-2 minutes where transfer time is unlikely to affect cadence."
        ),
    )
    parser.add_argument(
        "--read-exposure-every-n",
        type=int,
        default=0,
        help=(
            "Optional AP-mode exposure readback cadence. Default 0 disables "
            "readback during capture because some Olympus AP modes return blank "
            "values and each failed read can cost multiple seconds."
        ),
    )
    parser.add_argument(
        "--post-download-fullres",
        action="store_true",
        help="After a clean run, recover full-resolution JPEGs from the camera SD card.",
    )
    parser.add_argument(
        "--post-render",
        action="store_true",
        help="After full-resolution recovery, render the clean/brightness/director videos.",
    )
    parser.add_argument(
        "--post-copy-videos-to-mac",
        action="store_true",
        help="After rendering, copy MP4 outputs to the configured Mac destination.",
    )
    parser.add_argument(
        "--post-mac-video-dest",
        default=None,
        help="scp-style destination for rendered videos, e.g. user@host:/path.",
    )
    args = parser.parse_args()

    load_env_file(ENV_FILE)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path.cwd() / f"{stamp}_{args.mode}_v5_aperture_priority_wifi"
    paths = ensure_run_tree(run_dir)
    logger = RunLogger(run_dir)
    state = SharedState(TimelapseState(mode=args.mode, run_dir=str(run_dir)))
    camera = WifiCameraController(logger)
    commander = AICommander(logger)

    requested_duration_seconds = min(
        int(args.max_frames * args.interval),
        int(args.max_run_minutes * 60),
    )
    manifest = {
        "version": "5.0.9-aperture-priority-observer",
        "mode": args.mode,
        "run_dir": str(run_dir),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "interval_seconds": float(args.interval),
        "max_frames": int(args.max_frames),
        "max_run_minutes": float(args.max_run_minutes),
        "requested_duration_seconds": requested_duration_seconds,
        "transfer_mode": args.transfer_mode,
        "source": "scheduler_or_cli_aperture_priority_observer",
        "aperture_priority_observer": True,
        "baseline_override": {
            "iso": args.iso,
            "aperture": args.aperture,
            "shutter_seconds": args.shutter,
        },
        "postprocess": {
            "download_fullres_after": bool(args.post_download_fullres),
            "render_after": bool(args.post_render),
            "copy_videos_to_mac_after": bool(args.post_copy_videos_to_mac),
            "mac_video_dest": args.post_mac_video_dest,
        },
        "aperture_priority_readback": {
            "read_exposure_every_n": int(args.read_exposure_every_n),
        },
        "lens_profile": {
            "name": LENS_PROFILE_NAME,
            "physical_min_aperture": float(LENS_MIN_APERTURE),
            "physical_max_aperture": float(LENS_MAX_APERTURE),
            "control_min_aperture": float(MIN_APERTURE),
        },
        "exposure_policy": {
            "min_iso": int(MIN_ISO),
            "max_iso": int(MAX_ISO),
            "min_aperture": float(MIN_APERTURE),
            "max_aperture": float(MAX_APERTURE),
            "min_shutter_seconds": float(MIN_SHUTTER_SECONDS),
            "max_shutter_seconds": float(MAX_SHUTTER_SECONDS),
            "preferred_max_shutter_seconds": float(PREFERRED_MAX_SHUTTER_SECONDS),
        },
    }
    try:
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"WARN: could not write run_manifest.json: {exc}", flush=True)

    logger.human("=" * 68)
    logger.human(" TIMELAPSER V5.0.9 — APERTURE PRIORITY OBSERVER TEST")
    logger.human("=" * 68)
    logger.human(f"Mode:                 {args.mode}")
    logger.human(f"Capture interval:     {args.interval:.2f}s start-to-start")
    logger.human(f"Max run duration:     {args.max_run_minutes:.1f} min")
    logger.human("Camera transport:     olympuswifi over dedicated wlan1")
    logger.human("Exposure control:     camera body Aperture Priority; script does not write exposure")
    logger.human("Shutter sequence:     shutter mode -> 1st2ndpush -> 2nd1strelease")
    logger.human("File discovery:       predict next JPEG; list_images only on recovery")
    logger.human(f"Transfer mode:        {args.transfer_mode.upper()}")
    if any(v is not None for v in (args.iso, args.aperture, args.shutter)):
        logger.human(
            "CLI baseline override: "
            f"ISO {args.iso if args.iso is not None else 'mode default'} | "
            f"f/{args.aperture if args.aperture is not None else 'mode default'} | "
            f"shutter {args.shutter if args.shutter is not None else 'mode default'}s"
        )
    if args.transfer_mode == "thumbnail":
        logger.human("Transfer guidance:    best for 5-30s cadence; full JPEG stays on SD")
    else:
        logger.human("Transfer guidance:    best for long cadence (~1-2 min+); full JPEG copied to Pi")
        logger.human("Transfer warning:     a stalled full download can delay the next exposure")
    logger.human("Image policy:         JPEG-only recommended to reduce camera/SD workload")
    logger.human("AI authority:         disabled for AP test; holy-grail shadow still logged")
    logger.human(
        f"Lens profile:         {LENS_PROFILE_NAME}; physical widest f/{LENS_MIN_APERTURE:g}; "
        f"control floor f/{MIN_APERTURE:g}"
    )
    logger.human("Exposure observation: read back ISO/aperture/shutter after each frame")
    logger.human(f"Run directory:        {run_dir}")
    logger.human("=" * 68)
    clear_stale_stop_request(logger)

    normal_finish = False
    exit_code = 0
    run_started = time.monotonic()
    scene_history = deque(maxlen=720)  # ~60 min at 5 s cadence
    shadow_hg = ShadowHolyGrailController() if HOLY_GRAIL_SHADOW_ENABLED else None

    try:
        current = camera.open()
        state.update(lambda s: (
            setattr(s, "iso", int(current["iso"])),
            setattr(s, "aperture", float(current["aperture"])),
            setattr(s, "shutter_seconds", float(current["shutter_seconds"])),
        ))
        logger.human(
            "AP observer: leaving camera exposure/mode untouched. "
            "Set Aperture Priority, aperture, ISO, white balance and focus on the camera body."
        )
        logger.event("aperture_priority_observer_started", initial=current)

        next_capture_at = time.monotonic()

        while state.snapshot()["next_frame_no"] <= args.max_frames:
            if apply_operator_stop_if_requested(state, logger):
                break

            snap = state.snapshot()
            if snap["abort_requested"]:
                logger.human(f"AI requested abort: {snap['abort_reason']}")
                break
            if time.monotonic() - run_started >= args.max_run_minutes * 60:
                logger.human("Maximum run duration reached.")
                normal_finish = True
                break

            if apply_operator_stop_if_requested(state, logger):
                break

            snap = state.snapshot()
            if snap["abort_requested"]:
                logger.human(f"AI requested abort: {snap['abort_reason']}")
                break

            now = time.monotonic()
            if now < next_capture_at:
                remaining = next_capture_at - now
                if remaining > 0:
                    time.sleep(min(0.1, remaining))
                continue

            snap = state.snapshot()
            frame_no = snap["next_frame_no"]
            started = time.monotonic()

            try:
                result, local_jpg, metrics, timings = capture_download_analyse(
                    camera, paths, frame_no, logger, args.transfer_mode
                )
            except Exception as exc:
                logger.error("capture_download_analyse", exc, frame_no=frame_no)
                raise

            def success(s):
                s.physical_frames_requested += 1
                s.downloaded_frames += 1
                s.next_frame_no += 1
                s.last_remote_jpg = result.remote_jpg
                s.last_remote_orf = result.remote_orf
                s.last_local_jpeg = str(local_jpg)
                s.last_preview_jpeg = str(local_jpg)
                s.last_image_metrics = metrics
            state.update(success)
            if args.read_exposure_every_n > 0 and frame_no % args.read_exposure_every_n == 0:
                snap = refresh_observed_exposure(camera, state, logger)
            else:
                snap = state.snapshot()

            scene_history.append({
                "monotonic": time.monotonic(),
                "frame": frame_no,
                "median": float(metrics.get("median", 0.0) or 0.0),
                "highlights_pct": float(metrics.get("highlights_pct", 0.0) or 0.0),
                "shadows_pct": float(metrics.get("shadows_pct", 0.0) or 0.0),
            })
            scene_trend = summarise_scene_trend(scene_history)
            state.update(lambda s: setattr(s, "scene_trend", scene_trend))

            holy_grail_shadow = {}
            if shadow_hg is not None:
                current_settings = ExposureSettings(
                    iso=int(snap["iso"]),
                    aperture=float(snap["aperture"]),
                    shutter_seconds=float(snap["shutter_seconds"]),
                )
                shadow_hg.push_frame(frame_no, time.monotonic(), metrics, current_settings)
                holy_grail_shadow = shadow_hg.recommend(args.mode, current_settings)
                state.update(lambda s: setattr(s, "holy_grail_shadow", holy_grail_shadow))

            snap["scene_trend"] = scene_trend
            logger.telemetry({
                "frame": frame_no,
                "time": datetime.now().isoformat(timespec="milliseconds"),
                "remote_jpg": result.remote_jpg,
                "remote_orf": result.remote_orf or "",
                "discovery_method": getattr(result, "discovery_method", "unknown"),
                "local_jpeg": local_jpg.name,
                "iso": snap["iso"],
                "aperture": snap["aperture"],
                "shutter_seconds": snap["shutter_seconds"],
                "scene_trend": scene_trend.get("classification"),
                "brightness_delta_1m": scene_trend.get("delta_1m"),
                "brightness_delta_5m": scene_trend.get("delta_5m"),
                "hg_shadow_action": holy_grail_shadow.get("action"),
                "hg_shadow_delta_ev": holy_grail_shadow.get("recommended_delta_ev"),
                "hg_shadow_scene_ev": holy_grail_shadow.get("scene_ev"),
                "hg_shadow_scene_slope_ev_per_minute": holy_grail_shadow.get("scene_slope_ev_per_minute"),
                "hg_shadow_anomaly": holy_grail_shadow.get("latest_is_anomaly"),
                "hg_shadow_iso": (holy_grail_shadow.get("proposed") or {}).get("iso"),
                "hg_shadow_aperture": (holy_grail_shadow.get("proposed") or {}).get("aperture"),
                "hg_shadow_shutter_seconds": (holy_grail_shadow.get("proposed") or {}).get("shutter_seconds"),
                **timings,
                **metrics,
            })
            logger.human(
                f"Frame {frame_no:04d}: {Path(result.remote_jpg).name} -> {local_jpg.name} | "
                f"median={metrics.get('median', float('nan')):.3f} | "
                f"discover={getattr(result, 'discovery_method', 'unknown')} | "
                f"capture={timings['camera_capture_seconds']:.2f}s "
                f"{'thumb' if timings['transfer_mode'] == 'thumbnail' else 'full'}="
                f"{timings['transfer_seconds']:.2f}s "
                f"analyse={timings['image_analysis_seconds']:.2f}s | "
                f"trend={scene_trend.get('classification', 'unknown')} | "
                f"HG-shadow={holy_grail_shadow.get('action', 'off')} "
                f"{holy_grail_shadow.get('recommended_delta_ev', '')}"
            )

            next_capture_at += args.interval
            if next_capture_at < time.monotonic() - args.interval:
                next_capture_at = time.monotonic()

        else:
            normal_finish = True

    except KeyboardInterrupt:
        exit_code = 130
        logger.human("Stopped by user.")
        logger.event("user_interrupt")
    except Exception as exc:
        exit_code = 1
        logger.error("main", exc)
        logger.human(f"FATAL: {type(exc).__name__}: {exc}")
    finally:
        commander.stop()
        snap = state.snapshot()
        summary = {
            "version": "5.0.9-aperture-priority-observer",
            "mode": args.mode,
            "physical_frames_requested": snap["physical_frames_requested"],
            "downloaded_frames": snap["downloaded_frames"],
            "last_remote_jpg": snap["last_remote_jpg"],
            "last_remote_orf": snap["last_remote_orf"],
            "last_local_jpeg": snap["last_local_jpeg"],
            "abort_requested": snap["abort_requested"],
            "abort_reason": snap["abort_reason"],
            "normal_finish": normal_finish,
            "exit_code": exit_code,
        }
        logger.write_summary(summary)

        logger.human("")
        logger.human("=" * 68)
        logger.human(" V5 APERTURE PRIORITY OBSERVER RUN ENDED")
        logger.human("=" * 68)
        logger.human(f"Physical exposures:    {snap['physical_frames_requested']}")
        logger.human(f"JPEGs downloaded:      {snap['downloaded_frames']}")
        logger.human(f"Last camera JPEG:      {snap['last_remote_jpg']}")
        logger.human(f"Last camera ORF:       {snap['last_remote_orf']}")
        logger.human(f"Run log:               {logger.run_log_path}")
        logger.human(f"Telemetry:             {logger.telemetry_path}")
        logger.human(f"Summary:               {logger.summary_path}")

        if normal_finish and (
            args.post_download_fullres or args.post_render or args.post_copy_videos_to_mac
        ):
            logger.human("")
            logger.human("=" * 68)
            logger.human(" V5 POST-RUN WORKFLOW")
            logger.human("=" * 68)
            post_summary = run_postprocess(
                run_dir,
                download_fullres=args.post_download_fullres,
                render=args.post_render,
                copy_videos_to_mac=args.post_copy_videos_to_mac,
                mac_video_dest=args.post_mac_video_dest,
            )
            logger.event("postprocess_workflow_completed", summary=post_summary)
            logger.human(f"Postprocess status:    {post_summary.get('status')}")
            logger.human(f"Postprocess log:       {run_dir / 'postprocess_workflow.log'}")
            summary["postprocess"] = post_summary
            logger.write_summary(summary)
        elif not normal_finish and (
            args.post_download_fullres or args.post_render or args.post_copy_videos_to_mac
        ):
            logger.human("Postprocess skipped because this was not a clean/normal finish.")

        if normal_finish:
            try:
                camera.power_down()
            except Exception:
                pass
        camera.close()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
