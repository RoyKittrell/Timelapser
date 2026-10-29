#!/usr/bin/env python3
"""Configuration shared by the wired Timelapser V6 components."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
CONTROL_DIR = PROJECT_ROOT / "control_v6"
DEFAULT_OUTPUT_ROOT = Path(os.environ.get("TIMELAPSER_OUTPUT_ROOT", "/media/roy/My Passport/Timelapser"))

# Verified 2026-10-09 against the installed relay modules and live camera:
# GPIO23 drives AF and GPIO24 drives release. Both relay inputs assert HIGH and
# must idle LOW. Environment overrides remain available for future rewiring.
FOCUS_GPIO = int(os.environ.get("TIMELAPSER_V6_FOCUS_GPIO", "23"))
SHUTTER_GPIO = int(os.environ.get("TIMELAPSER_V6_SHUTTER_GPIO", "24"))

RELAY_ACTIVE_LOW = False
WAKE_PULSE_SECONDS = float(os.environ.get("TIMELAPSER_V6_WAKE_PULSE_SECONDS", "0.5"))
WAKE_SETTLE_SECONDS = float(os.environ.get("TIMELAPSER_V6_WAKE_SETTLE_SECONDS", "2.0"))
FOCUS_PULSE_SECONDS = float(os.environ.get("TIMELAPSER_V6_FOCUS_PULSE_SECONDS", "2.0"))
FOCUS_SETTLE_SECONDS = float(os.environ.get("TIMELAPSER_V6_FOCUS_SETTLE_SECONDS", "0.75"))
HALF_PRESS_CLEAR_SECONDS = float(os.environ.get("TIMELAPSER_V6_HALF_PRESS_CLEAR_SECONDS", "1.0"))
SHUTTER_PULSE_SECONDS = float(os.environ.get("TIMELAPSER_V6_SHUTTER_PULSE_SECONDS", "0.15"))
MIN_INTERVAL_SECONDS = float(os.environ.get("TIMELAPSER_V6_MIN_INTERVAL_SECONDS", "0.5"))

# Optional convenience delivery. Keep disabled while the dashboard and external
# drive are the primary destinations; set the environment value to 1 to restore it.
COPY_VIDEOS_TO_MAC = os.environ.get("TIMELAPSER_V6_COPY_VIDEOS_TO_MAC", "0").strip().lower() in {
    "1", "true", "yes", "on",
}

STOP_REQUEST = CONTROL_DIR / "stop_requested.json"
ACTIVE_RUN = CONTROL_DIR / "active_run.json"
SCHEDULER_PAUSE_FILE = CONTROL_DIR / "scheduler_paused.json"
PREFLIGHT_ALERT_FILE = CONTROL_DIR / "director_preflight_alert.json"
CAMERA_ALIVE_STATE = CONTROL_DIR / "camera_alive.json"
CAMERA_OPERATION_LOCK = CONTROL_DIR / "camera_operation.lock"
TEST_SHOT_DIR = CONTROL_DIR / "test_shots"
TEST_SHOT_IMAGE = TEST_SHOT_DIR / "latest.jpg"
TEST_SHOT_STATE = TEST_SHOT_DIR / "latest.json"
TEST_SHOT_MAX_AGE_SECONDS = float(
    os.environ.get("TIMELAPSER_V6_TEST_SHOT_MAX_AGE_SECONDS", "86400")
)

# A successful physical camera check in the preflight window can be reused at
# mission start. The normal scheduler preflight is ten minutes before capture.
CAMERA_ALIVE_MAX_AGE_SECONDS = float(
    os.environ.get("TIMELAPSER_V6_CAMERA_ALIVE_MAX_AGE_SECONDS", "900")
)
CAMERA_ALIVE_DEVICE_TIMEOUT_SECONDS = float(
    os.environ.get("TIMELAPSER_V6_CAMERA_ALIVE_DEVICE_TIMEOUT_SECONDS", "45")
)
# Total time after the shutter pulse for the aperture-priority exposure and SD
# write to complete before AF cleanup. Night testing currently peaks near six
# seconds, so 15 seconds provides comfortable headroom without delaying preflight.
CAMERA_ALIVE_CAPTURE_SETTLE_SECONDS = float(
    os.environ.get("TIMELAPSER_V6_CAMERA_ALIVE_CAPTURE_SETTLE_SECONDS", "15")
)
CAMERA_ALIVE_USB_SETTLE_SECONDS = float(
    os.environ.get("TIMELAPSER_V6_CAMERA_ALIVE_USB_SETTLE_SECONDS", "20")
)
