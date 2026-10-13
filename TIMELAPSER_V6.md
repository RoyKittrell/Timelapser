# Timelapser V6.0

V6 replaces live Olympus Wi-Fi capture with two isolated wired remote relays.
The Raspberry Pi closes the AF contact once at startup, releases it, then uses
only the RLS contact for the timed sequence. The camera writes every image to
its own SD card.

## Capture contract

- Camera common connects to COM on both isolated relays.
- Camera AF and RLS connect to the corresponding NO terminals.
- Relay NC terminals are unused.
- GPIO drives only the isolated relay modules, never camera contacts directly.
- AF is BCM GPIO23 and release is BCM GPIO24.
- Both module inputs assert HIGH and must be held LOW at process startup,
  failure, signal handling, and normal shutdown.
- Startup first holds AF for 0.5 seconds to wake a sleeping camera, releases it,
  and waits 2 seconds. It then holds AF for 2 seconds to focus, releases it, and
  allows a 0.75 second settling period before shutter-only capture begins.
- The RM-CB2 interface retains a half-press state after shutter-only pulses.
  V6 therefore performs a 1 second AF pulse after the final frame, including
  interrupted runs, to clear that state before releasing the GPIO controller.
- `/home/roy/.config/timelapser/v6-hardware.env` can override the assignments
  if the physical wiring changes later.

The mapping was recovered from the successful hardware commissioning thread on
7 October 2026, where GPIO23 produced AF and GPIO24 produced shutter release.

## Data flow

```text
schedule_v6.py
  -> timelapser_scheduler_v6.py
  -> timelapser_v6.py
     -> autofocus once
     -> monotonic shutter pulses
     -> trigger_timeline.csv (no images or live EXIF)
  -> timelapser_postprocess_v6.py
     -> MEGA4 camera USB on and mount
     -> copy newest N JPEGs, where N is successful trigger count
     -> build telemetry.csv from JPEG EXIF and measured brightness
     -> smooth, render, format and publish using the proven V5 tail
     -> MEGA4 camera USB off
```

Sunrise and sunset runs publish automatically. General runs process and remain
on disk without Instagram publishing.

## Dashboard

`timelapser_director_v6.py` shows only information available without camera
images: scheduler state, run state, trigger count, cadence, relay mapping and
post-run stage. It has no live frame, test shot, live brightness, camera API,
Wi-Fi, or live exposure controls.

Exposure and brightness graphs are withheld until USB import, rendering and
Instagram upload have all completed. They are then built from the downloaded
full-resolution JPEGs.

## First hardware commissioning

1. Confirm the installed wiring still matches GPIO23 AF and GPIO24 RLS after
   any enclosure or cable work. Do not infer this from wire color.
2. Install `v6-hardware.env.example` at the private Pi path shown above.
3. Run a simulation before hardware:
   `python3 timelapser_v6.py --simulate --max-frames 5 --interval 1`.
4. With the camera disconnected, verify both relay outputs are open at idle and
   each intended relay alone closes during its pulse.
5. Connect the camera and run a short attended three-frame general mission.
6. Confirm AF runs once, RLS fires three times, the camera records exactly three
   JPEGs, and both contacts release afterward.
7. Test USB ingest with Instagram publishing disabled before enabling services.


Install only after steps 1 and 2:

```bash
sudo bash ./install-v6.sh
```

## Current deliberate limitation

The USB importer maps the newest `N` monotonically named Olympus JPEGs to the
`N` successful wired triggers. This is deterministic when no one takes extra
photos between capture and import. A later revision should snapshot the SD
inventory before capture or add camera-side sequence markers if unattended
interleaving ever becomes possible.

## Exposure-aware relay cadence

The camera owns Aperture Priority exposure, while V6 cannot read it live.
`v6_exposure_model.py` therefore predicts shutter speed from minutes relative
to the scheduled solar event. Its baseline curves came from full-JPEG EXIF in
the successful 2026-10-01 sunrise and 2026-09-30 sunset runs. After each clean
V6 import, `v6_exposure_learning.py` rebuilds a calibration from up to 12 recent
compatible runs. It accepts only complete imports near ISO 200 and f/5.6,
groups samples into two-minute solar-event bins, and uses the 90th percentile
shutter speed. The prediction is always the slower of the baseline and learned
curves, so learning cannot shorten a previously safe cadence.

Sunrise and sunset captures never trigger faster than the schedule's base
interval. The model lengthens spacing when predicted exposure plus camera busy
time exceeds that interval, including an extra exposure-sized allowance above
four seconds for observed long-exposure noise-reduction behavior. `general`
mode remains fixed cadence. `--fixed-cadence` disables adaptation for attended
tests, and `--event-time` provides the timezone-aware solar-event anchor.
The learned calibration is stored atomically at
`~/.local/share/timelapser/v6_exposure_calibration.json` and its path is
recorded as the exposure-model source in subsequent run manifests.
