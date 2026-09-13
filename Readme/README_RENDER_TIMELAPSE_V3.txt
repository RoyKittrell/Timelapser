TIMELAPSER VIDEO RENDERER V3.1
===============================

Changes from V3:
- Director brightness chart now uses the same title-left/value-right layout as brightness-only.
- Removed the redundant Director SCENE row.
- Brightness-only chart occupies most of the safe frame by default.
- Director camera-update notices are generated only from exposure settings that actually became active in telemetry; rejected AI proposals are never shown.
- All layout remains user-editable in overlay_config.py.

TIMELAPSER VIDEO RENDERER V3 — LIVE OVERLAYS
============================================

V3 creates multiple synchronized Instagram timelapse videos from the same exact
telemetry-selected Olympus JPEG sequence.

DEFAULT OUTPUTS
---------------
1. *_instagram-reel_clean.mp4
   Normal 1080x1920 Instagram Reel timelapse.

2. *_instagram-reel_brightness.mp4
   Exact same video with a LIVE scene-brightness graph. The graph reveals only
   samples that have happened up to the current movie frame — it does not reveal
   the future. Current brightness is shown numerically.

3. *_instagram-reel_director.mp4
   Exact same video with an AI Director telemetry HUD: frame/time, ISO, aperture,
   shutter speed, measured scene trend, current brightness, and the same live graph.

READABILITY
-----------
All text is white with a thin black edge. The brightness trace is also white with a
black edge so it remains legible over bright sky, clouds, buildings, or darkness.

USER-EDITABLE OVERLAY CONFIG
----------------------------
overlay_config.py is deliberately separate from the renderer.

Edit it to change:
- left/right/top/bottom safe margins
- font sizes
- black text-outline thickness
- graph position and size
- graph line/edge thickness
- panel opacity
- y-axis range
- Director text visibility
- default output videos

Most dimensions are FRACTIONS of the final video width/height so the layout scales
between 1080x1920 and portrait 4K.

Safe-area enforcement prevents graph geometry from accidentally spilling outside the
configured margins.

INSTALL
-------
The overlay renderer needs Pillow as well as FFmpeg:

    source /home/roy/timelapser-venv/bin/activate
    pip install pillow
    sudo apt install ffmpeg

NORMAL COMMAND
--------------
    cd "/home/roy/Timelapser Sept2026/timelapser_v5"
    source /home/roy/timelapser-venv/bin/activate

    python3 render_timelapse.py \
      "/home/roy/Timelapser Sept2026/timelapser_v5/RUN_FOLDER" \
      --source "/home/roy/Timelapser Sept2026/timelapser_v5/RUN_FOLDER/frames_full_jpeg"

That makes clean + brightness + director videos by default.

ONLY BRIGHTNESS VIDEO
---------------------
    python3 render_timelapse.py RUN_DIR --source FULL_JPEG_DIR --outputs brightness

ONLY DIRECTOR VIDEO
-------------------
    python3 render_timelapse.py RUN_DIR --source FULL_JPEG_DIR --outputs director

CLEAN + BRIGHTNESS
------------------
    python3 render_timelapse.py RUN_DIR --source FULL_JPEG_DIR --outputs clean brightness

DRY RUN
-------
    python3 render_timelapse.py RUN_DIR --source FULL_JPEG_DIR --dry-run

This validates exact JPEG mapping and also verifies that a brightness field such as
'median' exists when an overlay render is requested.

IMPLEMENTATION
--------------
- telemetry.csv remains authoritative for production frame selection.
- Source JPEGs are symlink-staged; never modified/deleted.
- Overlay graphics are generated as temporary transparent PNGs.
- FFmpeg scales/crops the original JPEG and composites the transparent overlay.
- H.264/libx264, yuv420p and +faststart remain the output path.
- Pi 5 uses software x264; preset defaults to veryfast.
- H.264 level is selected dynamically: 4.2 for 1080-class output, 5.1 for 4K-class.
- Temporary overlay PNGs are deleted after each successful/failed render unless
  KEEP_OVERLAY_FRAMES=True in overlay_config.py or --keep-stage is supplied.

TELEMETRY
---------
Brightness overlay looks first for these columns:
    median
    median_brightness
    brightness_median
    luminance_median

Director also uses, when present:
    time
    iso
    aperture
    shutter_seconds
    scene_trend

Missing optional Director fields display as a dash; missing brightness is considered
fatal for brightness/director output because the graph would be meaningless.
