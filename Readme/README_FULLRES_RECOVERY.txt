TIMELAPSER FULL-RES JPEG RECOVERY
=================================

Purpose
-------
Run this only AFTER a V5 timelapse has stopped.

The capture script records the exact Olympus JPEG filename for every production
frame in telemetry.csv. download_run_fullres.py treats that telemetry as the
authoritative manifest and retrieves those exact full-resolution JPEGs.

It does NOT infer the sequence from the first/last filename.

Output
------
<RUN_DIR>/frames_full_jpeg/frame_000001.jpg
<RUN_DIR>/frames_full_jpeg/frame_000002.jpg
...

<RUN_DIR>/fullres_manifest.csv
<RUN_DIR>/fullres_download.log
<RUN_DIR>/fullres_download_summary.json

Safety/reliability
------------------
- Never deletes SD-card files.
- Uses .part + atomic rename.
- Validates JPEG SOI/EOI markers.
- Computes SHA-256 for every completed master.
- Re-verifies file size + hash after writing.
- Resumes after Ctrl+C/reboot.
- Recreates olympuswifi session after failed transfer.
- Retries each file.
- Repeats repair passes over gaps.
- Cross-checks telemetry against run_summary.
- Checks telemetry frame continuity and duplicate mappings.
- Optionally inventories SD once at startup.
- Uses a Linux hard timeout to escape a wedged Olympus HTTP transfer.

Recommended command
-------------------
source /home/roy/timelapser-venv/bin/activate

python3 download_run_fullres.py \
  "/home/roy/Timelapser Sept2026/timelapser_v5/<RUN_FOLDER>"

Or newest run:

python3 download_run_fullres.py --latest

Useful options
--------------
--attempts-per-frame 4
--repair-passes 5
--operation-timeout 120
--skip-inventory
--force-redownload

Resume behavior
---------------
Rerun the same command. Files that have a complete manifest record and pass
size + SHA-256 verification are skipped. Failed/missing files are retried.

Success condition
-----------------
The utility only exits 0 when the final independent verification pass confirms:

    SESSION VERIFIED COMPLETE
    Expected frames:     N
    Verified full JPEGs: N
    Missing/invalid:     0

Integration with the main timelapser
------------------------------------
Do not automatically chain this into V5 until it has been physically tested.
Full JPEG transfer can be much slower than capture and Olympus Wi-Fi is known to
stall occasionally.

Once validated, timelapser_v5.py can invoke this as a separate subprocess after
camera capture has fully stopped, so the downloader remains isolated from the
real-time camera loop.
