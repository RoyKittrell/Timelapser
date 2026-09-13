#!/usr/bin/env python3
"""
schedule.py
===========

Human-editable mission plan for Timelapser V5.

Edit THIS file to control scheduled sunrise/sunset runs. The scheduler imports
DEFAULTS and SCHEDULE from here.

Times are local Kuala Lumpur time (Asia/Kuala_Lumpur).

Design goals:
- deterministic, inspectable schedule
- no internet dependency at runtime
- defaults for sunrise/sunset
- per-event overrides
- easy enable/disable
"""

TIMEZONE = "Asia/Kuala_Lumpur"

# How early the scheduler should preflight each event.
PREFLIGHT_MINUTES_BEFORE_START = 10

# If the Pi starts/reboots after an event start but before event end, the
# scheduler may start the event late rather than abandoning the day.
START_LATE_IF_STILL_ACTIVE = True

# Do not start a badly-late event if less than this many minutes remain.
MIN_REMAINING_MINUTES_TO_START = 1

# Main V5 script and Python interpreter.
PYTHON = "/home/roy/timelapser-venv/bin/python3"
TIMELAPSER_SCRIPT = "/home/roy/Timelapser Sept2026/timelapser_v5/timelapser_v5_aperture_priority.py"

# Optional post-run commands. These run only after capture stops cleanly.
DEFAULT_DOWNLOAD_FULLRES_AFTER = True
DEFAULT_RENDER_AFTER = True
DEFAULT_COPY_VIDEOS_TO_MAC_AFTER = True
DEFAULT_MAC_VIDEO_DEST = "roy@192.168.100.191:/Users/roy/Documents/ChatGPT/Timelapser/rendered_videos"

# Exposure/capture defaults.
# IMPORTANT:
# These are defaults only. Per-event entries below may override any value.
DEFAULTS = {
    "sunrise": {
        "start_offset_minutes": -40,
        "end_offset_minutes": 50,
        "interval": 5,
        "mode": "sunrise",
        "iso": 200,
        "aperture": 5.0,
        "shutter": 1.0,
        "transfer_mode": "thumbnail",
        "max_frames": None,
        "download_fullres_after": DEFAULT_DOWNLOAD_FULLRES_AFTER,
        "render_after": DEFAULT_RENDER_AFTER,
        "copy_videos_to_mac_after": DEFAULT_COPY_VIDEOS_TO_MAC_AFTER,
        "mac_video_dest": DEFAULT_MAC_VIDEO_DEST,
    },
    "sunset": {
        "start_offset_minutes": -45,
        "end_offset_minutes": 70,
        "interval": 5,
        "mode": "sunset",
        "iso": 200,
        "aperture": 6.3,
        "shutter": 1 / 125,
        "transfer_mode": "thumbnail",
        "max_frames": None,
        "download_fullres_after": DEFAULT_DOWNLOAD_FULLRES_AFTER,
        "render_after": DEFAULT_RENDER_AFTER,
        "copy_videos_to_mac_after": DEFAULT_COPY_VIDEOS_TO_MAC_AFTER,
        "mac_video_dest": DEFAULT_MAC_VIDEO_DEST,
    },
    "general": {
        "start_offset_minutes": 0,
        "end_offset_minutes": 60,
        "interval": 5,
        "mode": "general",
        "iso": 200,
        "aperture": 6.3,
        "shutter": 1 / 125,
        "transfer_mode": "thumbnail",
        "max_frames": None,
        "download_fullres_after": DEFAULT_DOWNLOAD_FULLRES_AFTER,
        "render_after": DEFAULT_RENDER_AFTER,
        "copy_videos_to_mac_after": DEFAULT_COPY_VIDEOS_TO_MAC_AFTER,
        "mac_video_dest": DEFAULT_MAC_VIDEO_DEST,
    },
}

# ---------------------------------------------------------------------------
# CHRONOLOGICAL MISSION PLAN
# ---------------------------------------------------------------------------
#
# Add rows in date/time order.
#
# Required:
#   date       YYYY-MM-DD
#   event      sunrise or sunset
#   event_time HH:MM local clock time
#
# Optional overrides:
#   enabled, start_offset_minutes, end_offset_minutes, interval, mode,
#   iso, aperture, shutter, transfer_mode, max_frames,
#   download_fullres_after, render_after, copy_videos_to_mac_after,
#   mac_video_dest, notes
#
# Example override:
# {
#     "date": "2026-09-12",
#     "event": "sunset",
#     "event_time": "19:14",
#     "end_offset_minutes": 90,
#     "notes": "Long blue-hour test",
# }
#
# Generated from Timeanddate Kuala Lumpur sunrise/sunset tables.
SCHEDULE = [
    {"date": "2026-09-13", "event": "sunrise", "event_time": "07:05", "enabled": True},
    {"date": "2026-09-13", "event": "sunset", "event_time": "19:13", "enabled": True},
    {"date": "2026-09-14", "event": "sunrise", "event_time": "07:04", "enabled": True},
    {"date": "2026-09-14", "event": "sunset", "event_time": "19:12", "enabled": True},
    {"date": "2026-09-15", "event": "sunrise", "event_time": "07:04", "enabled": True},
    {"date": "2026-09-15", "event": "sunset", "event_time": "19:12", "enabled": True},
    {"date": "2026-09-16", "event": "sunrise", "event_time": "07:04", "enabled": True},
    {"date": "2026-09-16", "event": "sunset", "event_time": "19:11", "enabled": True},
    {"date": "2026-09-17", "event": "sunrise", "event_time": "07:04", "enabled": True},
    {"date": "2026-09-17", "event": "sunset", "event_time": "19:11", "enabled": True},
    {"date": "2026-09-18", "event": "sunrise", "event_time": "07:03", "enabled": True},
    {"date": "2026-09-18", "event": "sunset", "event_time": "19:11", "enabled": True},
    {"date": "2026-09-19", "event": "sunrise", "event_time": "07:03", "enabled": True},
    {"date": "2026-09-19", "event": "sunset", "event_time": "19:10", "enabled": True},
    {"date": "2026-09-20", "event": "sunrise", "event_time": "07:03", "enabled": True},
    {"date": "2026-09-20", "event": "sunset", "event_time": "19:10", "enabled": True},
    {"date": "2026-09-21", "event": "sunrise", "event_time": "07:02", "enabled": True},
    {"date": "2026-09-21", "event": "sunset", "event_time": "19:09", "enabled": True},
    {"date": "2026-09-22", "event": "sunrise", "event_time": "07:02", "enabled": True},
    {"date": "2026-09-22", "event": "sunset", "event_time": "19:09", "enabled": True},
    {"date": "2026-09-23", "event": "sunrise", "event_time": "07:02", "enabled": True},
    {"date": "2026-09-23", "event": "sunset", "event_time": "19:08", "enabled": True},
    {"date": "2026-09-24", "event": "sunrise", "event_time": "07:02", "enabled": True},
    {"date": "2026-09-24", "event": "sunset", "event_time": "19:08", "enabled": True},
    {"date": "2026-09-25", "event": "sunrise", "event_time": "07:01", "enabled": True},
    {"date": "2026-09-25", "event": "sunset", "event_time": "19:07", "enabled": True},
    {"date": "2026-09-26", "event": "sunrise", "event_time": "07:01", "enabled": True},
    {"date": "2026-09-26", "event": "sunset", "event_time": "19:07", "enabled": True},
    {"date": "2026-09-27", "event": "sunrise", "event_time": "07:01", "enabled": True},
    {"date": "2026-09-27", "event": "sunset", "event_time": "19:07", "enabled": True},
    {"date": "2026-09-28", "event": "sunrise", "event_time": "07:01", "enabled": True},
    {"date": "2026-09-28", "event": "sunset", "event_time": "19:06", "enabled": True},
    {"date": "2026-09-29", "event": "sunrise", "event_time": "07:00", "enabled": True},
    {"date": "2026-09-29", "event": "sunset", "event_time": "19:06", "enabled": True},
    {"date": "2026-09-30", "event": "sunrise", "event_time": "07:00", "enabled": True},
    {"date": "2026-09-30", "event": "sunset", "event_time": "19:05", "enabled": True},
    {"date": "2026-10-01", "event": "sunrise", "event_time": "07:00", "enabled": True},
    {"date": "2026-10-01", "event": "sunset", "event_time": "19:05", "enabled": True},
    {"date": "2026-10-02", "event": "sunrise", "event_time": "07:00", "enabled": True},
    {"date": "2026-10-02", "event": "sunset", "event_time": "19:05", "enabled": True},
    {"date": "2026-10-03", "event": "sunrise", "event_time": "06:59", "enabled": True},
    {"date": "2026-10-03", "event": "sunset", "event_time": "19:04", "enabled": True},
    {"date": "2026-10-04", "event": "sunrise", "event_time": "06:59", "enabled": True},
    {"date": "2026-10-04", "event": "sunset", "event_time": "19:04", "enabled": True},
    {"date": "2026-10-05", "event": "sunrise", "event_time": "06:59", "enabled": True},
    {"date": "2026-10-05", "event": "sunset", "event_time": "19:03", "enabled": True},
    {"date": "2026-10-06", "event": "sunrise", "event_time": "06:59", "enabled": True},
    {"date": "2026-10-06", "event": "sunset", "event_time": "19:03", "enabled": True},
    {"date": "2026-10-07", "event": "sunrise", "event_time": "06:59", "enabled": True},
    {"date": "2026-10-07", "event": "sunset", "event_time": "19:03", "enabled": True},
    {"date": "2026-10-08", "event": "sunrise", "event_time": "06:58", "enabled": True},
    {"date": "2026-10-08", "event": "sunset", "event_time": "19:02", "enabled": True},
    {"date": "2026-10-09", "event": "sunrise", "event_time": "06:58", "enabled": True},
    {"date": "2026-10-09", "event": "sunset", "event_time": "19:02", "enabled": True},
    {"date": "2026-10-10", "event": "sunrise", "event_time": "06:58", "enabled": True},
    {"date": "2026-10-10", "event": "sunset", "event_time": "19:02", "enabled": True},
    {"date": "2026-10-11", "event": "sunrise", "event_time": "06:58", "enabled": True},
    {"date": "2026-10-11", "event": "sunset", "event_time": "19:01", "enabled": True},
    {"date": "2026-10-12", "event": "sunrise", "event_time": "06:58", "enabled": True},
    {"date": "2026-10-12", "event": "sunset", "event_time": "19:01", "enabled": True},
    {"date": "2026-10-13", "event": "sunrise", "event_time": "06:57", "enabled": True},
    {"date": "2026-10-13", "event": "sunset", "event_time": "19:01", "enabled": True},
    {
        'date': '2026-09-13',
        'event': 'general',
        'event_time': '14:08',
        'enabled': True,
        'start_offset_minutes': 0,
        'end_offset_minutes': 60,
        'interval': 5.0,
        'mode': 'general',
        'transfer_mode': 'thumbnail',
        'max_frames': 720,
        'notes': 'Director one-off: Schedule a time lapse to start two minutes from now',
    },
]
