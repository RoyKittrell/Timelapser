# Current Timelapser setup

This document describes the production V6 arrangement without recording private
network addresses, dashboard links, credentials, or operator-specific paths.
Local values belong in environment files excluded by `.gitignore`.

## Capture hardware

- Raspberry Pi 5 running the scheduler, Director dashboard, GPIO capture,
  post-processing, retention, and publishing services.
- Olympus E-M5 Mark III with a 64 GB SD card.
- Modified Olympus-compatible remote cable exposing common, autofocus, and
  shutter contacts.
- Two isolated active-HIGH 5 V relay modules: BCM GPIO23 for AF and BCM GPIO24
  for shutter release. Both outputs idle LOW.
- MEGA4-controlled USB data connection used only when the camera SD card must
  be exposed for verification or import.
- External archive drive for run folders, source JPEGs, rendered media, and
  permanent run records.
- SHT3X enclosure sensor plus Raspberry Pi CPU-temperature telemetry.

## Camera profile

The production capture mode is Aperture Priority with fixed framing and manual
focus. The currently learned exposure epoch accepts Auto ISO 200–400, an
aperture near f/5.6–f/5.8, and the camera's preferred two-second slow-shutter
setting. That Olympus shutter setting is a preference, not a hard limit.

Digital ESP is the recommended metering mode because it evaluates the whole
scene. Spot metering would make the sequence more sensitive to a cloud,
reflection, or building detail crossing one small metering area. White balance
should be fixed rather than Auto; 5200 K or 5400 K are both suitable starting
points and should be selected consistently in camera.

The lens is kept in PreMF/manual focus for the distant subject. Scheduled runs
still issue a short AF-contact wake pulse but skip the separate autofocus
operation, preserving the manual focus distance.

## Scheduled windows

The defaults intentionally keep the extra half hour on the brighter side of
each event:

- Sunrise: 40 minutes before through 80 minutes after the event.
- Sunset: 75 minutes before through 70 minutes after the event.

Sunrise and sunset runs may publish automatically. General/manual runs remain
local unless the operator explicitly approves Reel-only publishing.

## Capture and processing

1. Ten-minute preflight verifies storage, services, GPIO configuration, and a
   real test exposure. The test JPEG is downloaded for the dashboard, removed
   from the camera, and automatically expires from the dashboard after 24
   hours.
2. Capture uses monotonic relay timing. Sunrise and sunset cadence may lengthen
   from the requested five-second minimum when the learned shutter model says
   the camera will still be exposing or writing.
3. Capture takes priority over processing. If a scheduled mission becomes due,
   the active post-process is stopped safely; partial render outputs are
   discarded and the resumable workflow restarts that stage later.
4. Full JPEGs are imported and validated before rendering. The camera SD card
   is erased only after the complete required workflow succeeds.
5. Source-frame brightness correction produces 3840-edge JPEGs. Clean Reels
   render at 2160×3840, H.264 High Level 5.1, CRF 16, slow preset, constrained
   to 20 Mb/s, BT.709 limited range, with restrained sharpening. Overlay
   masters render at high-quality 1080×1920 before 4:5 carousel preparation.
6. Free-space-aware retention begins below 180 GiB free and aims for 240 GiB.
   It removes regenerable intermediates before source JPEGs, while preserving
   manifests, telemetry, logs, EXIF data, captions, graphs, catalog records,
   and publication receipts.
7. Dashboard Photos requests are pulled by a Mac-side agent and assigned to the
   configured Photos album. Host names, paths, SSH keys, and destinations are
   supplied through environment variables rather than committed values.

## Public-repository boundary

The repository contains source, tests, service templates, examples, and
non-secret technical findings. It deliberately excludes media, run folders,
raw camera research captures, credentials, environment files, dashboard URLs,
private network destinations, operator notes, and machine-specific state.
