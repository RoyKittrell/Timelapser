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
FOCUS_PULSE_SECONDS = float(os.environ.get("TIMELAPSER_V6_FOCUS_PULSE_SECONDS", "2.0"))
FOCUS_SETTLE_SECONDS = float(os.environ.get("TIMELAPSER_V6_FOCUS_SETTLE_SECONDS", "0.75"))
HALF_PRESS_CLEAR_SECONDS = float(os.environ.get("TIMELAPSER_V6_HALF_PRESS_CLEAR_SECONDS", "1.0"))
SHUTTER_PULSE_SECONDS = float(os.environ.get("TIMELAPSER_V6_SHUTTER_PULSE_SECONDS", "0.15"))
MIN_INTERVAL_SECONDS = float(os.environ.get("TIMELAPSER_V6_MIN_INTERVAL_SECONDS", "0.5"))

STOP_REQUEST = CONTROL_DIR / "stop_requested.json"
ACTIVE_RUN = CONTROL_DIR / "active_run.json"
SCHEDULER_PAUSE_FILE = CONTROL_DIR / "scheduler_paused.json"
PREFLIGHT_ALERT_FILE = CONTROL_DIR / "director_preflight_alert.json"
