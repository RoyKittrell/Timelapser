#!/usr/bin/env python3
"""Human-editable mission plan for wired Timelapser V6.

The mission list and timing defaults remain in schedule.py so there is still
one calendar to edit. V6 replaces only the capture and post-capture engines.
Legacy exposure fields are retained for schedule compatibility but are ignored
during wired capture; the camera owns exposure locally. Reload schedule.py
when this adapter is re-executed so the persistent scheduler sees timing edits
without requiring a service restart.
"""

import importlib
from pathlib import Path

import schedule as _schedule


importlib.reload(_schedule)
from schedule import *  # noqa: F401,F403


PROJECT_ROOT = Path(__file__).resolve().parent
TIMELAPSER_SCRIPT = str(PROJECT_ROOT / "timelapser_v6.py")
POSTPROCESS_SCRIPT = str(PROJECT_ROOT / "timelapser_postprocess_v6.py")

