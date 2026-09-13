TIMELAPSER V6 BETA - USB STORAGE IMPORT
=======================================

Purpose
-------
V6 beta keeps the stable V5 Wi-Fi capture path, but uses USB Storage mode for
fast post-run full-resolution JPEG import.

Current physical finding
------------------------
When the E-M5 Mark III is connected to the Pi and set to USB Storage:

- Linux exposes it as a USB mass-storage disk, currently /dev/sda.
- The card mounts at /media/roy/5B48-8907.
- The camera files are visible under DCIM/100OLYMP.
- The Olympus Wi-Fi API at 192.168.0.10 refuses connections while the camera is
  in USB Storage mode.

That means simultaneous V5 Wi-Fi capture plus USB Storage import is not currently
available. The safe workflow is:

1. Capture the timelapse normally over Wi-Fi.
2. After the run stops, connect USB and choose Storage on the camera.
3. Run the V6 beta USB importer.
4. Eject/unmount the camera storage before unplugging.
5. Return the camera to Wi-Fi/control mode before the next scheduled capture.

Importer
--------
timelapser_v6_beta_usb_import.py maps V5 telemetry rows to the full JPEGs on the
mounted SD card. It does not guess by first/last filename. It checks Olympus
basename plus EXIF DateTime against telemetry time, because Olympus filenames can
be reused across runs.

Dry run newest run:

    /home/roy/timelapser-venv/bin/python3 timelapser_v6_beta_usb_import.py --latest --dry-run

Import one run:

    /home/roy/timelapser-venv/bin/python3 timelapser_v6_beta_usb_import.py \
      20260911_182903_sunset_v5_wifi --replace-thumbnails

Scan all runs and import only exact matches:

    /home/roy/timelapser-venv/bin/python3 timelapser_v6_beta_usb_import.py \
      --all --replace-thumbnails

Outputs
-------
For each complete imported run:

    <RUN_DIR>/frames_full_jpeg/frame_000001.jpg
    <RUN_DIR>/frames_full_jpeg/frame_000002.jpg
    <RUN_DIR>/frames_full_jpeg/usb_fullres_copy_manifest.csv

By default it does not delete thumbnails. With --replace-thumbnails, it deletes
<RUN_DIR>/frames_jpeg/*.jpg only after every telemetry frame maps successfully.

Exit codes
----------
0 = all requested runs imported or dry-run mapped completely.
2 = at least one requested run was incomplete/mismatched and was skipped/failed.

Notes
-----
This is intentionally post-run only. It avoids the old fragile USB PTP/gphoto2
capture path entirely.
