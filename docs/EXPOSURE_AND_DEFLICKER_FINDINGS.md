# Exposure and deflicker findings

## Production correction

The production V6 workflow performs source-frame correction before video
encoding. It measures downscaled frame luminance, smooths the sequence with a
61-frame window over three passes, limits correction to ±0.25 stop, and applies
the result once to 3840-edge JPEGs. Camera originals are never modified.

This substantially improves individual exposure jumps, but a global luminance
curve cannot distinguish genuine scene evolution from spatially uneven flicker
caused by metering, clouds, haze, local reflections, or in-camera JPEG tone
processing.

## 7 November general-run investigation

The investigated sequence contained 1,200 JPEGs:

- 883 frames at 1/1250 s.
- 317 frames at 1/1600 s.
- ISO 200 throughout.

The shutter ratio predicts a 0.356-stop difference, but robust adjacent A-B-A
transitions measured only about 0.1398 stop in the rendered JPEGs. Applying a
fixed correction based solely on EXIF shutter state did not solve the flicker.
Two visibly different adjacent frames even shared the same ISO, aperture, and
1/1600 s shutter value, proving that EXIF state alone is insufficient.

For that representative pair, the original JPEG median brightness differed by
about 4.7%. The existing Pi source-frame correction reduced the difference to
about 0.3%, and the corresponding encoded production frames differed by about
0.1%. The pair was corrected well, yet perceptible flicker remained elsewhere
in the sequence.

A second experiment measured nine image regions and applied only the common
temporal residual. Its actual adjustment was small (approximately -0.0268 to
+0.0263 stop; 95th-percentile absolute correction 0.0142 stop). It reduced the
95th-percentile common adjacent jump from 0.03447 to 0.02320 stop—roughly one
third—but the resulting video was still not visually acceptable.

## Conclusions

- Do not infer brightness correction from shutter speed alone.
- Do not deploy either experimental correction in `experiments/deflicker/` as
  the production pipeline.
- JPEG brightness can change at identical exposure metadata, likely because of
  scene metering, sensor/JPEG processing, or tone-curve behavior that EXIF does
  not record.
- The next useful benchmark should be motion-aware or spatially varying
  temporal deflicker, compared against the current source-frame pipeline on the
  same frame sequence.

Candidate references include [LRTimelapse](https://lrtimelapse.com/), DaVinci
Resolve Studio's Deflicker effect, Digital Anarchy Flicker Free, the CVPR 2023
[All-In-One Deflicker](https://github.com/chenyanglei/all-in-one-deflicker)
research implementation, and the ECCV 2024 BlazeBVD illumination-histogram and
exposure-map approach. These are research/benchmark directions, not current Pi
dependencies.

## Camera-meter telemetry research

The camera's Digital ESP system evaluates a multi-area scene and must produce
an internal exposure target before choosing discrete shutter/ISO steps. A
firmware-research path could first log that target—or the zone measurements,
histogram, selected exposure, and JPEG gain—without changing exposure control.
Only after reliable read-only telemetry exists should a fine-grained control
loop be considered.

No firmware patch is part of the production system. Firmware work risks
bricking the camera and belongs on a spare body with exact firmware hashes and
a verified recovery path. The safer prototype is an external HDMI or image
measurement loop that estimates scene EV while the normal wired trigger remains
the capture authority.
