# Timelapser

AI-controlled timelapse photography using a Raspberry Pi and an Olympus E-M5 Mark III camera.

Timelapser is an experimental autonomous camera-control system for shooting sunrise, sunset and general timelapses. A Raspberry Pi connects to the camera's dedicated Olympus/OI.Share Wi-Fi network, triggers stills, downloads preview images for analysis, logs every frame, and uses a constrained AI Director plus deterministic guardrails to reason about exposure and cadence.

The current V5 system is focused on reliability first: keep the camera loop simple, avoid duplicate frames after flaky acknowledgements, preserve forensic logs, and run heavier full-resolution downloads and video rendering only after capture has stopped.

## Current Status

This repository contains the actively tested Timelapser V5 codebase.

V5 currently supports:

- Olympus Wi-Fi camera control via the `olympuswifi` Python package.
- Scheduled sunrise, sunset and one-off timelapse missions.
- A Streamlit AI Director dashboard for live telemetry, recent frames, run status, storage checks and operator commands.
- Thumbnail-based in-run analysis to protect short capture intervals.
- Post-run full-resolution JPEG recovery from the camera SD card.
- Post-run video rendering, including clean, brightness-overlay and Director-overlay versions.
- An aperture-priority observer mode for testing whether the camera body can ramp exposure more smoothly than scripted full-manual control.
- Early V6 beta experiments around USB storage import. V6 is not the production path yet.

## Hardware

The system was built around:

- Raspberry Pi running the timelapser project.
- Olympus E-M5 Mark III camera.
- Dedicated Wi-Fi adapter for the camera network.
- Normal LAN/Wi-Fi interface for SSH, dashboard access and GitHub.
- 64 GB camera SD card.
- Optional external hard drive attached to the Pi for archiving completed runs.

Typical network layout:

- `wlan0`: normal LAN, SSH, Tailscale, internet.
- `wlan1`: Olympus/OI.Share camera Wi-Fi network, usually `192.168.0.x`.
- Camera HTTP endpoint: `http://192.168.0.10/`.

## Architecture

The main capture flow is:

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

Important files:

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
- `timelapser_v6_beta_usb_import.py`: experimental USB storage import work.

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
3. Attempt to copy rendered videos to the configured Mac destination.

The Mac copy step requires SSH from the Pi to the Mac. If macOS Remote Login is disabled, the render still completes on the Pi and the copy failure is recorded in the postprocess log.

Manual postprocess rerun:

```bash
cd "/home/roy/Timelapser Sept2026/timelapser_v5"
source /home/roy/timelapser-venv/bin/activate
python3 timelapser_postprocess_v5.py "/path/to/run_folder"
```

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
