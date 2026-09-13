TIMELAPSER SCHEDULER V1
=======================

FILES
-----
schedule.py
    Human-editable chronological sunrise/sunset mission plan.

timelapser_scheduler.py
    Scheduler process. Reloads schedule.py every ~20 seconds.

timelapser-scheduler.service
    Optional systemd service for unattended startup after Pi reboot.


V1.1 EXPOSURE OVERRIDE PATCH
----------------------------
This bundle now includes a patched timelapser_v5.py based on V5.0.6. It adds:

    --iso
    --aperture
    --shutter

These are optional STARTING baseline overrides. If omitted, V5.0.6 keeps its
normal sunrise/sunset baseline behaviour. The AI Director/governor still owns
subsequent exposure decisions exactly as before.


INSTALL
-------
Copy schedule.py and timelapser_scheduler.py into:

    /home/roy/Timelapser Sept2026/timelapser_v5/


STATUS / DRY OPERATION
----------------------
See what the scheduler thinks will happen:

    cd "/home/roy/Timelapser Sept2026/timelapser_v5"
    source /home/roy/timelapser-venv/bin/activate
    python3 timelapser_scheduler.py --status

Run one scheduler decision tick and exit:

    python3 timelapser_scheduler.py --once

Run interactively:

    python3 timelapser_scheduler.py


HOW IT WORKS
------------
1. schedule.py contains a chronological mission list.
2. Each mission provides date + sunrise/sunset clock time.
3. Defaults supply start/end offsets, interval and starting exposure.
4. Scheduler calculates:
       preflight time
       capture start
       capture end
5. Ten minutes before start it runs preflight checks.
6. At start it launches timelapser_v5.py as a child process.
7. It stores persistent event status in scheduler_state.json.
8. If the Pi reboots during an active event window, the scheduler can start the
   remaining portion of that event instead of abandoning it.
9. schedule.py is reloaded on every scheduler tick, so edits do not require a
   scheduler restart.


EDITING THE SCHEDULE
--------------------
Defaults are near the top of schedule.py.

The chronological list looks like:

    SCHEDULE = [
        {
            "date": "2026-09-11",
            "event": "sunrise",
            "event_time": "07:03",
            "enabled": True,
        },
        {
            "date": "2026-09-11",
            "event": "sunset",
            "event_time": "19:14",
            "enabled": True,
            "end_offset_minutes": 90,
        },
    ]

Any default can be overridden in an individual mission.


SYSTEMD AUTOSTART
-----------------
Only do this after interactive testing succeeds.

    sudo cp timelapser-scheduler.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now timelapser-scheduler.service

Check:

    systemctl status timelapser-scheduler.service

Live log:

    journalctl -u timelapser-scheduler.service -f

Disable:

    sudo systemctl disable --now timelapser-scheduler.service


PERSISTENT STATE
----------------
scheduler_state.json is automatically written beside the script.

Typical states:
    pending
    armed
    running
    complete
    failed
    preflight_failed
    missed

Do not normally hand-edit this file while the scheduler is running.


CURRENT SAFETY BOUNDARY
-----------------------
V1 does NOT yet:
- power-cycle the Olympus
- automatically re-enter camera Wi-Fi after a hard camera reboot
- download full-resolution masters after capture
- render the three Instagram videos after capture
- send failure notifications

Those should be layered on after the basic scheduler has proven reliable.
