# Timelapser

AI-controlled timelapse photography using a Raspberry Pi and an Olympus E-M5 Mark III camera.

Timelapser is an experimental autonomous camera-control system for shooting sunrise, sunset and general timelapses. The current V6 development path triggers the camera through a hacked wired intervalometer cable and two isolated relays. The camera writes images locally to its SD card; the Pi imports and analyses them only after capture has finished, then renders videos and optionally publishes eligible sunrise and sunset runs.

The proven V5 Wi-Fi implementation remains in the repository as a fallback and as a record of the earlier live-thumbnail architecture.

## Current Status

This repository contains both the proven V5 Wi-Fi implementation and the current V6 wired-capture development line.

V6 currently provides:

- Wired autofocus and shutter control through active-HIGH 5 V relay modules.
- An AF wake pulse and settle delay at mission startup, followed by a two-second autofocus operation, shutter-only frame capture, and a final AF pulse that clears the RM-CB2 half-press state.
- Deterministic monotonic capture timing without dependence on Olympus Wi-Fi.
- Sunrise/sunset relay cadence predicted from successful Aperture Priority EXIF curves, with conservative spacing for long exposures and camera busy time.
- Post-run camera USB mounting and full-resolution JPEG import.
- Post-import EXIF extraction, brightness analysis, rendering and Instagram publishing.
- A simplified dashboard that reports only information available during wired capture; image-derived graphs appear after import.
- Scheduled sunrise, sunset and general missions. Sunrise and sunset runs may publish automatically; general runs remain local.

V5 remains available and supports:

- Olympus Wi-Fi camera control via the `olympuswifi` Python package.
- Scheduled sunrise, sunset and one-off timelapse missions.
- A Streamlit AI Director dashboard for live telemetry, recent frames, run status, storage checks and operator commands.
- Thumbnail-based in-run analysis to protect short capture intervals.
- Post-run full-resolution JPEG recovery from the camera SD card.
- Post-run video rendering, including clean, brightness-overlay and Director-overlay versions.
- An aperture-priority observer mode for testing whether the camera body can ramp exposure more smoothly than scripted full-manual control.
- The established Wi-Fi recovery, rendering and publishing workflow that V6 reuses after capture.

## Hardware

The V6 system is built around:

- Raspberry Pi running the timelapser project.
- Olympus E-M5 Mark III camera.
- A compatible wired remote/intervalometer cable modified to expose camera common, autofocus and shutter conductors.
- Two 5 V relay modules providing isolated dry-contact AF and shutter switching.
- A LAN/Wi-Fi interface for SSH, dashboard access, uploads and GitHub.
- 64 GB camera SD card.
- Optional external hard drive attached to the Pi for archiving completed runs.

The older V5 network layout used separate home and Olympus Wi-Fi radios. V6 does not require camera Wi-Fi for capture.

## Wired Intervalometer Interface

V6 requires an Olympus-compatible intervalometer/remote cable that exposes three camera-side conductors. A commercial intervalometer can be opened and modified, or an equivalent cable can be made from a compatible camera plug. Confirm the conductors with a continuity meter instead of assuming wire colours.

Camera-side relay contacts:

```text
Camera common  -> COM on both relays
Camera AF      -> NO on the autofocus relay
Camera shutter -> NO on the shutter relay
NC terminals   -> unused
```

Pi control-side wiring:

```text
AF relay DC+       -> physical pin 2, 5 V
AF relay DC-       -> physical pin 14, GND
AF relay IN        -> physical pin 16, BCM GPIO23

Shutter relay DC+  -> physical pin 4, 5 V
Shutter relay DC-  -> physical pin 20, GND
Shutter relay IN   -> physical pin 18, BCM GPIO24
```

The installed modules are active-HIGH: HIGH closes the relay and LOW is the safe idle state. Camera common is part of the isolated contact side and must not be connected to Pi ground. Pi 5 V must never be connected to the camera-side `COM`, `NO` or `NC` terminals.

The commissioned sequence first pulses AF for 0.5 seconds to wake a sleeping camera and waits two seconds. It then holds AF for two seconds, returns GPIO23 LOW, waits 0.75 seconds, and uses shutter-only pulses on GPIO24. This prevents the camera from refocusing every frame while allowing normal camera sleep between missions. The RM-CB2 interface retains half-press after shutter-only operation, so V6 finishes with a one-second AF pulse and returns both GPIOs LOW.

## V6 Architecture

The wired capture flow is:

1. Scheduler or Director starts a mission.
2. V6 creates the run manifest and trigger timeline.
3. GPIO23 wakes the camera, waits for it to become ready, and performs one startup autofocus operation.
4. GPIO24 triggers each frame while the camera stores JPEGs on its SD card.
5. After capture, the camera is exposed as USB storage and the newest expected JPEG sequence is imported.
6. EXIF and brightness telemetry are extracted from the imported images.
7. Clean and overlay videos are rendered in the established Instagram formats.
8. Eligible sunrise and sunset missions publish automatically; general missions remain on disk.

For scheduled sunrise and sunset missions, V6 anchors a fitted exposure curve
to the configured solar-event time. The model was derived from the successful
2026-10-01 sunrise and 2026-09-30 sunset Aperture Priority runs at ISO 200 and
approximately f/5.6. It never shortens the requested interval, but lengthens
relay spacing when the predicted exposure and camera busy time require it.
General missions retain fixed cadence. Predictions are logged beside every
trigger for later comparison with imported EXIF; a future ambient-light sensor
can replace the prediction layer without changing capture ownership.

Important V6 files:

- `timelapser_v6.py`: wired capture runner.
- `v6_intervalometer.py`: safe AF and shutter relay controller.
- `v6_config.py`: GPIO assignments, autofocus timing and capture defaults.
- `timelapser_v6_usb_import.py`: deterministic post-run JPEG importer.
- `timelapser_postprocess_v6.py`: import, analysis, render and publish orchestration.
- `timelapser_scheduler_v6.py` and `schedule_v6.py`: mission scheduling.
- `timelapser_director_v6.py`: simplified wired-capture dashboard.
- `install-v6.sh`: V6 service installation.

Detailed commissioning and safety notes are in `TIMELAPSER_V6.md`.

## V5 Wi-Fi Architecture

Typical V5 network layout:

- `wlan0`: normal LAN, SSH, Tailscale, internet.
- `wlan1`: Olympus/OI.Share camera Wi-Fi network, usually `192.168.0.x`.
- Camera HTTP endpoint: `http://192.168.0.10/`.

### Capture Flow

The V5 capture flow is:

1. Scheduler or Director starts a run.
2. The runner creates a timestamped run directory.
3. The camera controller connects over Olympus Wi-Fi.
4. Each frame is captured on the camera.
5. A small JPEG thumbnail is downloaded during the run.
6. Local image analysis measures brightness, highlights and shadows.
7. The AI Commander reviews telemetry and image context asynchronously.
8. Deterministic guardrails decide whether any exposure change is allowed.
9. Telemetry, camera commands, AI decisions, events and errors are written to disk.
10. After a clean finish, optional post-processing downloads full JPEGs and renders videos.

Important V5 files:

- `timelapser_v5.py`: primary full-manual V5 runner.
- `timelapser_v5_aperture_priority.py`: aperture-priority observer runner.
- `v5_camera.py`: Olympus Wi-Fi camera transport and recovery logic.
- `v5_commander.py`: AI Commander prompts, parsing and constrained actions.
- `v5_holygrail.py`: deterministic Holy Grail comparison/recommendation logic.
- `v5_image.py`: local JPEG analysis.
- `v5_logging.py`: run logging, telemetry and forensic JSONL logs.
- `v5_state.py`: shared run state.
- `timelapser_scheduler.py`: persistent scheduler.
- `schedule.py`: human-editable mission plan.
- `timelapser_director_v5.py`: Streamlit dashboard and chat/operator interface.
- `download_run_fullres.py`: robust post-run full-resolution JPEG downloader.
- `render_timelapse.py`: clean/overlay video renderer.
- `timelapser_postprocess_v5.py`: post-run workflow wrapper.
- `instagram_formats.py`: preserves the 9:16 masters and creates full-frame 4:5 carousel copies.
- `instagram_auto_publish.py`: resumable carousel + Reels-only publisher for completed runs or manual folders.
- `install-camera-wifi-connect.sh`: one-time setup for passwordless, camera-only Wi-Fi activation during scheduler preflight.

Manual publishing uses the same four filenames as a completed run. Put one
`*instagram-carousel_clean.mp4`, `*instagram-carousel_director.mp4`,
`*instagram-carousel_brightness.mp4`, and `*instagram-reel_clean.mp4` in a
folder with `caption.txt`, then run:

```bash
python3 instagram_auto_publish.py "/path/to/folder"
```

Use `--dry-run` to validate the folder without contacting Instagram. A local
`instagram_publish_receipt.json` prevents successful parts from being posted
twice when an interrupted run is resumed.
- `timelapser_v6_beta_usb_import.py`: preserved early USB-storage prototype.

More detailed notes live in `Readme/`.

## Camera Control

Timelapser V5 talks to the Olympus camera through the same general Wi-Fi/HTTP control surface used by Olympus OI.Share-compatible bodies.

The production capture sequence is roughly:

- switch to record mode for settings/readback where needed
- switch to shutter mode for physical capture
- send `exec_shutter` press/release commands
- predict or discover the new JPEG path on the SD card
- download a thumbnail or full JPEG by Olympus file path

The code is intentionally conservative around lost acknowledgements. A network error during shutter control does not necessarily mean the exposure failed. V5 therefore reconnects, checks for the expected JPEG, performs stricter SD-card recovery when needed, and only then considers re-firing.

## Aperture-Priority Mode

`timelapser_v5_aperture_priority.py` is a current experiment. In this mode, the camera body is expected to be physically set to Aperture Priority. The script still triggers frames and records telemetry, but it does not intentionally write exposure settings during the run. It reads back observed ISO/aperture/shutter where possible so we can compare body-controlled ramping against Timelapser's full-manual approach.

If aperture-priority mode proves smoother, it may become the preferred production runner.

## Scheduler

Check upcoming missions:

```bash
cd "/home/roy/Timelapser Sept2026/timelapser_v5"
source /home/roy/timelapser-venv/bin/activate
python3 timelapser_scheduler.py --status
```

Run one scheduler tick:

```bash
python3 timelapser_scheduler.py --once
```

Run scheduler interactively:

```bash
python3 timelapser_scheduler.py
```

The scheduler reloads `schedule.py` repeatedly, so schedule edits do not normally require a scheduler restart.

## Dashboard

Start the Director dashboard:

```bash
cd "/home/roy/Timelapser Sept2026/timelapser_v5"
source /home/roy/timelapser-venv/bin/activate
streamlit run timelapser_director_v5.py
```

The dashboard shows live run telemetry, latest frames, exposure history, capture-cycle timing, logged errors, Wi-Fi state, Pi storage and estimated camera SD-card fullness.

The chat/operator interface can answer basic run/scheduler questions and can start or stop timelapses through constrained commands.

## Post-Run Workflow

After a clean scheduled run, the current default workflow is:

1. Download full-resolution JPEGs using `download_run_fullres.py`.
2. Render videos from `frames_full_jpeg/` using `render_timelapse.py`.
3. Prepare the clean Reel in `instagram_queue/ready/` with caption and metadata.
4. Attempt to copy rendered videos to the configured Mac destination, if enabled.

The Mac copy step requires SSH from the Pi to the Mac. If macOS Remote Login is disabled, the render still completes on the Pi and the copy failure is recorded in the postprocess log.

Manual postprocess rerun:

```bash
cd "/home/roy/Timelapser Sept2026/timelapser_v5"
source /home/roy/timelapser-venv/bin/activate
python3 timelapser_postprocess_v5.py "/path/to/run_folder"
```

## Instagram Publishing Scaffold

Instagram publishing is staged but not fully automatic until Meta and Dropbox
credentials are configured in the private environment file.

Prepare the clean Reel for posting:

```bash
python3 instagram_queue.py "/path/to/run_folder"
```

This creates a generated queue item below `instagram_queue/ready/` containing:

- clean Instagram Reel MP4
- `caption.txt`
- `metadata.json`
- `publish_status.json`

Check whether the private publishing credentials are available:

```bash
python3 instagram_publish.py --check-auth
```

Dry-run a queued item without uploading or publishing:

```bash
python3 instagram_publish.py instagram_queue/ready/RUN_FOLDER --dry-run
```

The live publishing flow will upload the Reel to Dropbox for a temporary public
URL, create an Instagram Reel media container, poll it until ready, and publish
only when explicitly run with `--publish`.

## Development Workflow

The Raspberry Pi project directory is now a Git repository:

```bash
cd "/home/roy/Timelapser Sept2026/timelapser_v5"
git status
```

Normal edit cycle:

```bash
git status
git add .
git commit -m "Describe the change"
git push
```

Update from GitHub:

```bash
git pull
```

Generated run folders, JPEGs, videos, logs, caches and local backups are ignored by `.gitignore`.

## Safety Notes

This project controls real camera hardware. Be careful with unattended changes.

Avoid doing these casually:

- triggering captures while another run is active
- changing camera settings during a live timelapse
- restarting services during a capture
- deleting run directories or SD-card files without checking first
- power-cycling the camera without preserving the run state

Most V5 code treats the SD card as read-only. Full-resolution recovery downloads files from the camera but does not delete or format the card.

## Third-Party Code And Credits

Timelapser's project code was developed by Roy Kittrell with assistance from OpenAI ChatGPT/Codex.

This repository depends on, references or interoperates with the following external projects and tools:

- [`olympuswifi`](https://github.com/joergmlpts/olympus-wifi) by Joerg Mueller, MIT licensed. Timelapser uses this Python package for Olympus Wi-Fi camera communication.
- The Olympus/OI.Share Wi-Fi control approach is based on the camera HTTP/CGI interface documented in the [`olympuswifi` documentation](https://olympus-wifi.readthedocs.io/en/stable/api.html) and the Olympus OPC Communication Protocol 1.0a notes referenced there.
- [`ccrome/olympus-omd-remote-control`](https://github.com/ccrome/olympus-omd-remote-control), which preserves useful Olympus O-MD remote-control protocol examples and links to the OPC protocol documentation.
- [OpenAI Python SDK](https://github.com/openai/openai-python), used for AI Commander/Director model calls.
- [Streamlit](https://streamlit.io/), used for the live Director dashboard.
- [OpenCV](https://opencv.org/), [NumPy](https://numpy.org/), [Pillow](https://python-pillow.org/) and [pandas](https://pandas.pydata.org/), used for image analysis, image handling, overlays and telemetry display.
- [FFmpeg](https://ffmpeg.org/), used by the video renderer.

No endorsement by OM Digital Solutions, Olympus, OpenAI or any other third-party project is implied.

## License

The Timelapser project code in this repository is published under CC0 1.0 Universal as indicated by `LICENSE`. Third-party dependencies remain under their own licenses.
