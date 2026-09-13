import time
from dataclasses import dataclass
from pathlib import Path

import olympuswifi.camera as olympuswifi_camera
from olympuswifi.camera import OlympusCamera as OlympusWifiCamera

from v5_config import (
    CARD_WRITE_WAIT_SECONDS,
    FILE_DISCOVERY_TIMEOUT_SECONDS,
    FILE_DISCOVERY_POLL_SECONDS,
    SHUTTER_HOLD_SECONDS,
    MODE_SETTLE_SECONDS,
    MIN_ISO,
    MAX_ISO,
    MIN_APERTURE,
    MAX_APERTURE,
    LENS_PROFILE_NAME,
    LENS_MIN_APERTURE,
    MIN_SHUTTER_SECONDS,
    MAX_SHUTTER_SECONDS,
    OLYMPUS_CGI_TIMEOUT_SECONDS,
    OLYMPUS_FULL_IMAGE_TIMEOUT_SECONDS,
    CAMERA_RECOVERY_WINDOW_SECONDS,
    CAMERA_RECOVERY_POLL_SECONDS,
)
from v5_bluetooth import try_wake_camera_wifi


# Exact values advertised by Roy's E-M1 Mark III over get_camprop?com=desc.
ISO_CHOICES = [200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600]
APERTURE_CHOICES = [
    2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6, 6.3, 7.1, 8.0
]

# (seconds, Olympus Wi-Fi string). Quoted entries are whole/multi-second
# exposures; unquoted values are reciprocal shutter-speed values.
SHUTTER_CHOICES = [
    (1/1000, "1000"), (1/800, "800"), (1/640, "640"),
    (1/500, "500"), (1/400, "400"), (1/320, "320"),
    (1/250, "250"), (1/200, "200"), (1/160, "160"),
    (1/125, "125"), (1/100, "100"), (1/80, "80"),
    (1/60, "60"), (1/50, "50"), (1/40, "40"),
    (1/30, "30"), (1/25, "25"), (1/20, "20"),
    (1/15, "15"), (1/13, "13"), (1/10, "10"),
    (1/8, "8"), (1/6, "6"), (1/5, "5"), (1/4, "4"),
    (1/3, "3"), (1/2.5, "2.5"), (1/2, "2"),
    (1/1.6, "1.6"), (1/1.3, "1.3"),
    (1.0, '1"'), (1.3, '1.3"'), (1.6, '1.6"'), (2.0, '2"'),
    (2.5, '2.5"'), (3.2, '3.2"'), (4.0, '4"'),
]


class _TimedRequests:
    """Add default timeouts to olympuswifi without modifying site-packages."""

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self._wrapped, name)

    def _timeout_for_url(self, url):
        text = str(url)
        if text.lower().endswith((".jpg", ".jpeg", ".orf")):
            return OLYMPUS_FULL_IMAGE_TIMEOUT_SECONDS
        return OLYMPUS_CGI_TIMEOUT_SECONDS

    def get(self, url, *args, **kwargs):
        kwargs.setdefault("timeout", self._timeout_for_url(url))
        return self._wrapped.get(url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        kwargs.setdefault("timeout", OLYMPUS_CGI_TIMEOUT_SECONDS)
        return self._wrapped.post(url, *args, **kwargs)


def _install_olympus_timeouts():
    requests_obj = olympuswifi_camera.requests
    if not isinstance(requests_obj, _TimedRequests):
        olympuswifi_camera.requests = _TimedRequests(requests_obj)


_install_olympus_timeouts()


@dataclass
class CaptureResult:
    remote_jpg: str
    remote_orf: str | None
    new_files: list
    discovery_method: str = "predictive"


class WifiCameraController:
    """E-M1 III controller using the Olympus/OI.Share Wi-Fi protocol.

    Proven sequence on this physical camera:
      rec     -> get/set exposure properties
      shutter -> exec_shutter 1st2ndpush, then 2nd1strelease
      play    -> direct predicted thumbnail/full-JPEG retrieval

    V5.0.6 indexes the SD card once at startup, predicts the next JPEG name, and
    contains transient Olympus HTTP resets so one dropped socket does not end the run.
    list_images() is retained only as an authoritative recovery fallback.
    """

    def __init__(self, logger):
        self.logger = logger
        self.cam = None
        self.mode = None
        self.known_names = set()
        self.current = {}
        self.last_remote_jpg = None
        self._thumbnail_cache = {}

    def _log_command(self, command, started, ok=True, **extra):
        self.logger.camera_command(
            transport="olympuswifi",
            command=command,
            duration_seconds=time.monotonic() - started,
            ok=ok,
            **extra,
        )

    def _switch_mode(self, mode):
        if self.mode == mode:
            return
        t0 = time.monotonic()
        try:
            self.cam.send_command("switch_cammode", mode=mode)
            self.mode = mode
            time.sleep(MODE_SETTLE_SECONDS)
            self._log_command(f"switch_cammode mode={mode}", t0)
        except Exception:
            self._log_command(f"switch_cammode mode={mode}", t0, ok=False)
            raise

    @staticmethod
    def _parse_shutter(raw):
        text = str(raw).strip()
        if text.endswith('"'):
            return float(text[:-1])
        return 1.0 / float(text)

    @staticmethod
    def _closest(values, requested):
        return min(values, key=lambda x: abs(float(x) - float(requested)))

    @staticmethod
    def _aperture_choices():
        return [x for x in APERTURE_CHOICES if MIN_APERTURE <= x <= MAX_APERTURE]

    def _lens_safe_aperture(self, aperture, *, context):
        aperture = float(aperture)
        if aperture >= float(MIN_APERTURE):
            return aperture
        self.logger.human(
            f"WARNING: Olympus reported aperture f/{aperture:g} below the "
            f"commandable V5 floor for {LENS_PROFILE_NAME}; treating it as "
            f"f/{MIN_APERTURE:g} for control."
        )
        self.logger.event(
            "wifi_lens_impossible_aperture_readback",
            context=context,
            reported_aperture=aperture,
            physical_min_aperture=float(LENS_MIN_APERTURE),
            control_min_aperture=float(MIN_APERTURE),
            lens_profile=LENS_PROFILE_NAME,
        )
        return float(MIN_APERTURE)

    @staticmethod
    def _closest_shutter(requested):
        # Compare in EV/log space so 1/125 vs 1/100 is treated sensibly.
        import math
        requested = max(MIN_SHUTTER_SECONDS, min(MAX_SHUTTER_SECONDS, float(requested)))
        return min(
            SHUTTER_CHOICES,
            key=lambda x: abs(math.log2(x[0]) - math.log2(requested)),
        )

    def open(self):
        # V5.0.7: startup is deliberately more conservative than an in-run mode
        # switch.  Olympus bodies can acknowledge REC mode before exposure props
        # are immediately readable, especially after a prior interrupted session.
        # V5.0.10: use the same bounded recovery window as in-run discovery so a
        # brief camera API wobble at startup does not abort an otherwise viable run.
        startup_delays = (1.0, 2.0, 4.0)
        startup_deadline = time.monotonic() + float(CAMERA_RECOVERY_WINDOW_SECONDS)
        last_exc = None
        attempt = 0
        self.logger.event(
            "wifi_startup_recovery_window_started",
            window_seconds=float(CAMERA_RECOVERY_WINDOW_SECONDS),
        )
        while time.monotonic() < startup_deadline:
            attempt += 1
            delay = startup_delays[min(attempt - 1, len(startup_delays) - 1)]
            try:
                self.cam = OlympusWifiCamera()
                self.mode = None
                t0 = time.monotonic()
                self.cam.send_command("switch_cammode", mode="rec")
                self.mode = "rec"
                time.sleep(delay)
                self._log_command(
                    f"switch_cammode mode=rec startup_attempt={attempt}",
                    t0,
                )

                raw_iso = self.cam.get_camprop("isospeedvalue")
                aperture_raw = self.cam.get_camprop("focalvalue")
                shutter_raw = self.cam.get_camprop("shutspeedvalue")
                break
            except Exception as exc:
                last_exc = exc
                remaining = max(0.0, startup_deadline - time.monotonic())
                self.logger.event(
                    "wifi_startup_rec_recovery",
                    attempt=attempt,
                    settle_seconds=delay,
                    recovery_seconds_remaining=remaining,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.logger.human(
                    f"Camera startup REC/read attempt {attempt} failed; "
                    f"{remaining:.0f}s recovery budget remains: {type(exc).__name__}: {exc}"
                )
                self.mode = None
                self.cam = None
                try_wake_camera_wifi(
                    self.logger,
                    reason=f"startup REC/read attempt {attempt}: {type(exc).__name__}",
                )
                time.sleep(min(float(CAMERA_RECOVERY_POLL_SECONDS), remaining))
        else:
            self.logger.event(
                "wifi_startup_recovery_window_exhausted",
                attempts=attempt,
                window_seconds=float(CAMERA_RECOVERY_WINDOW_SECONDS),
                last_error=(f"{type(last_exc).__name__}: {last_exc}" if last_exc else None),
            )
            raise last_exc

        try:
            iso = int(float(raw_iso))
        except (TypeError, ValueError):
            # E-M5 III can report symbolic values such as "Auto" or "Low".
            # open() only needs a provisional state; timelapser_v5.py immediately
            # applies a numeric baseline before the first production frame.
            iso = int(MIN_ISO)
            self.logger.human(
                f"Olympus reported symbolic ISO {raw_iso!r}; using provisional "
                f"ISO {iso} until numeric baseline is applied."
            )
            self.logger.event(
                "wifi_symbolic_iso_at_open",
                raw_iso=str(raw_iso),
                provisional_iso=iso,
            )
        aperture = self._lens_safe_aperture(aperture_raw, context="open")
        shutter = self._parse_shutter(shutter_raw)
        self.current = {
            "iso": iso,
            "aperture": aperture,
            "shutter_seconds": shutter,
        }

        # V5.0.9: index the SD card once at startup.  If the card has no JPEG,
        # bootstrap our own filename seed instead of requiring a human to press
        # the shutter.  This matters after a fresh/reformatted/emptied SD card.
        self._switch_mode("play")
        files = self.cam.list_images()
        self.known_names = {f.file_name for f in files}
        jpgs = [f for f in files if f.file_name.lower().endswith((".jpg", ".jpeg"))]
        if not jpgs:
            self._bootstrap_seed_jpeg()
        else:
            latest = max(jpgs, key=lambda f: f.date_time)
            self.last_remote_jpg = latest.file_name
        self._switch_mode("rec")

        self.logger.human(
            "Olympus Wi-Fi connected: "
            f"ISO {iso} | f/{aperture:g} | {shutter:.6g}s | "
            f"{len(self.known_names)} existing SD files indexed | "
            f"prediction seed {Path(self.last_remote_jpg).name}"
        )
        return dict(self.current)

    def _bootstrap_seed_jpeg(self):
        """Create a non-production JPEG when the SD card has no filename seed.

        Predictive capture needs one real Olympus filename to know what comes next.
        Older V5 builds aborted on an empty JPEG set and required a manual photo.
        V5.0.9 makes startup autonomous: take one setup exposure, then prove the
        resulting JPEG exists by authoritative SD-card listing.  The bootstrap
        exposure is deliberately not returned as a production frame.
        """
        baseline_names = set(self.known_names)
        retry_delays = (0.75, 2.0, 5.0)
        max_attempts = 3
        last_error = None

        self.logger.event(
            "wifi_seed_bootstrap_started",
            existing_files=len(baseline_names),
        )
        self.logger.human(
            "No JPEG exists on the camera SD card; creating one autonomous "
            "bootstrap exposure to seed filename prediction."
        )

        for attempt in range(1, max_attempts + 1):
            ack_state = self._fire_shutter(frame_no=0)
            if ack_state != "ok":
                self._recreate_client(
                    frame_no=0,
                    reason=f"bootstrap {ack_state} on attempt {attempt}",
                )

            time.sleep(CARD_WRITE_WAIT_SECONDS)

            try:
                self._switch_mode_with_recovery("play", frame_no=0)
                started = time.monotonic()
                listing = self.cam.list_images()
                self._log_command(
                    "list_images bootstrap_seed",
                    started,
                    bootstrap_attempt=attempt,
                    listed_files=len(listing),
                )
            except Exception as exc:
                last_error = exc
                self.logger.event(
                    "wifi_seed_bootstrap_listing_failed",
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                unseen = [f for f in listing if f.file_name not in baseline_names]
                jpgs = [
                    f for f in unseen
                    if f.file_name.lower().endswith((".jpg", ".jpeg"))
                ]
                if jpgs:
                    seed = max(jpgs, key=lambda f: f.date_time)
                    self.known_names = {f.file_name for f in listing}
                    self.last_remote_jpg = seed.file_name
                    self.logger.event(
                        "wifi_seed_bootstrap_completed",
                        attempt=attempt,
                        ack_state=ack_state,
                        remote_jpg=seed.file_name,
                        listed_files=len(listing),
                        unseen_files=[f.file_name for f in unseen],
                    )
                    self.logger.human(
                        "Autonomous bootstrap exposure created filename seed "
                        f"{Path(seed.file_name).name}; production capture can begin."
                    )
                    return seed.file_name

                last_error = RuntimeError(
                    "bootstrap shutter completed but no new JPEG appeared in SD listing"
                )
                self.logger.event(
                    "wifi_seed_bootstrap_no_jpeg",
                    attempt=attempt,
                    ack_state=ack_state,
                    listed_files=len(listing),
                    unseen_files=[f.file_name for f in unseen],
                )

            if attempt >= max_attempts:
                break

            delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
            self.logger.human(
                f"Bootstrap seed attempt {attempt}/{max_attempts} produced no "
                f"confirmed JPEG; retrying in {delay:g}s."
            )
            time.sleep(delay)
            self._recreate_client(
                frame_no=0,
                reason=f"preparing bootstrap seed retry {attempt + 1}",
            )

        self.logger.event(
            "wifi_seed_bootstrap_exhausted",
            attempts=max_attempts,
            error=(f"{type(last_error).__name__}: {last_error}" if last_error else None),
        )
        raise RuntimeError(
            "SD card has no JPEG and autonomous bootstrap could not create one after "
            f"{max_attempts} attempts. Confirm the camera is configured to save JPEGs."
        ) from last_error

    def close(self):
        # Do not send a power/off command on abort; just release the client.
        self.cam = None
        self.mode = None

    def _read_exposure_value(self, kind):
        if kind == "shutter":
            return self._parse_shutter(self.cam.get_camprop("shutspeedvalue"))
        if kind == "aperture":
            return self._lens_safe_aperture(
                self.cam.get_camprop("focalvalue"),
                context="set_exposure_readback",
            )
        if kind == "iso":
            return int(float(self.cam.get_camprop("isospeedvalue")))
        raise ValueError(kind)

    @staticmethod
    def _exposure_value_matches(kind, actual, target):
        if kind == "iso":
            return int(actual) == int(target)
        if kind == "aperture":
            return abs(float(actual) - float(target)) < 0.05
        # Shutter comparison in EV space; allow tiny float/representation error.
        import math
        return abs(math.log2(float(actual) / float(target))) < 0.03

    def set_exposure(self, *, iso=None, aperture=None, shutter_seconds=None):
        """Set exposure properties and verify every requested value by read-back.

        V5.0.7 optimistically updated local state immediately after set_camprop().
        V5.0.8 reads the property back from the body and retries the write once if
        it did not stick. Only a verified value is returned in ``applied``.
        """
        self._switch_mode("rec")
        applied = {}
        failed = []
        mismatched = []
        verified = {}

        sequence = (
            ("shutter", shutter_seconds),
            ("aperture", aperture),
            ("iso", iso),
        )

        for kind, requested in sequence:
            if requested is None:
                continue

            if kind == "shutter":
                target, sent = self._closest_shutter(requested)
                prop = "shutspeedvalue"
            elif kind == "aperture":
                target = self._closest(self._aperture_choices(), requested)
                sent = f"{target:.1f}"
                prop = "focalvalue"
            else:
                target = int(self._closest(
                    [x for x in ISO_CHOICES if MIN_ISO <= x <= MAX_ISO],
                    requested,
                ))
                sent = str(target)
                prop = "isospeedvalue"

            success = False
            last_actual = None
            last_exc = None
            for attempt in (1, 2):
                t0 = time.monotonic()
                try:
                    self.cam.set_camprop(prop, sent)
                    # Olympus property updates are normally quick, but a short settle
                    # avoids reading the previous value from the body immediately.
                    time.sleep(0.12)
                    last_actual = self._read_exposure_value(kind)
                    success = self._exposure_value_matches(kind, last_actual, target)
                    self._log_command(
                        f"set {kind}={sent} verify_attempt={attempt}",
                        t0,
                        ok=success,
                        requested=requested,
                        target=target,
                        readback=last_actual,
                    )
                    self.logger.event(
                        "wifi_exposure_readback",
                        kind=kind,
                        attempt=attempt,
                        requested=requested,
                        target=target,
                        sent=sent,
                        readback=last_actual,
                        verified=success,
                    )
                    if success:
                        break
                except Exception as exc:
                    last_exc = exc
                    self._log_command(
                        f"set {kind} verify_attempt={attempt}",
                        t0,
                        ok=False,
                        requested=requested,
                        target=target,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    self.logger.error(
                        "wifi_set_exposure", exc, kind=kind,
                        requested=requested, attempt=attempt,
                    )
                if attempt == 1:
                    time.sleep(0.15)

            if success:
                key = "shutter_seconds" if kind == "shutter" else kind
                applied[key] = target
                verified[key] = last_actual
                self.current[key] = target
            else:
                failed.append(kind)
                if last_actual is not None:
                    mismatched.append(kind)
                self.logger.event(
                    "wifi_exposure_verify_failed",
                    kind=kind,
                    requested=requested,
                    target=target,
                    readback=last_actual,
                    error=(f"{type(last_exc).__name__}: {last_exc}" if last_exc else None),
                )

        requested_kinds = [k for k, v in sequence if v is not None]
        ok = bool(requested_kinds) and not failed
        if not requested_kinds:
            ok = True
        return {
            "ok": ok,
            "applied": applied,
            "verified": verified,
            "failed_kinds": failed,
            "mismatched_kinds": mismatched,
            "requested_kinds": requested_kinds,
        }

    @staticmethod
    def _predict_next_jpeg(remote_jpg: str) -> str:
        """Predict the next Olympus filename by incrementing its numeric suffix.

        Example: /DCIM/100OLYMP/P9090085.JPG -> .../P9090086.JPG

        Folder/9999 rollover is deliberately not guessed here. If the prediction
        is wrong, the authoritative list_images() recovery path re-seeds us.
        """
        import re

        p = Path(remote_jpg)
        m = re.match(r"^(.*?)(\d+)$", p.stem)
        if not m:
            raise ValueError(f"Cannot predict next Olympus filename from {remote_jpg!r}")
        prefix, digits = m.groups()
        return str(p.with_name(f"{prefix}{int(digits) + 1:0{len(digits)}d}{p.suffix}"))

    def _probe_predicted_thumbnail(self, remote_jpg: str):
        """Fetch/cache the predicted thumbnail, retrying briefly while SD write settles.

        Success proves the predicted JPEG exists. The bytes are cached so the
        normal thumbnail-download stage does not hit the camera a second time.
        """
        started = time.monotonic()
        deadline = started + min(5.0, float(FILE_DISCOVERY_TIMEOUT_SECONDS))
        attempts = 0
        last_error = None

        while True:
            attempts += 1
            try:
                data = self.cam.download_thumbnail(remote_jpg)
                if not isinstance(data, (bytes, bytearray)) or not data:
                    raise RuntimeError(
                        f"download_thumbnail returned {type(data).__name__} / empty payload"
                    )
                data = bytes(data)
                self._thumbnail_cache[remote_jpg] = data
                self._log_command(
                    f"predictive_thumbnail_probe {remote_jpg}",
                    started,
                    attempts=attempts,
                    bytes=len(data),
                )
                return data
            except Exception as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(0.20, float(FILE_DISCOVERY_POLL_SECONDS)))

        self._log_command(
            f"predictive_thumbnail_probe {remote_jpg}",
            started,
            ok=False,
            attempts=attempts,
            error=f"{type(last_error).__name__}: {last_error}" if last_error else "unknown",
        )
        return None

    def _recreate_client(self, *, frame_no=None, reason="transient camera error",
                         attempts=3, raise_on_failure=True):
        """Discard the possibly-poisoned HTTP client without touching capture state.

        OlympusCamera is effectively a lightweight HTTP client.  After a peer reset,
        reusing its requests/session state is less attractive than creating a fresh
        instance.  Filename prediction state and cached telemetry state are retained.
        """
        delays = (0.75, 2.0, 5.0)
        last_exc = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            self.cam = None
            self.mode = None
            time.sleep(0.10)
            try:
                self.cam = OlympusWifiCamera()
                self.logger.event(
                    "wifi_client_recreated",
                    frame_no=frame_no,
                    reason=str(reason),
                    attempt=attempt,
                )
                self.logger.human(
                    f"Frame {frame_no:04d}: recreated Olympus Wi-Fi client after {reason}."
                    if frame_no is not None
                    else f"Recreated Olympus Wi-Fi client after {reason}."
                )
                return True
            except Exception as exc:
                last_exc = exc
                self.logger.event(
                    "wifi_client_recreate_failed",
                    frame_no=frame_no,
                    reason=str(reason),
                    attempt=attempt,
                    attempts=attempts,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self.logger.human(
                    f"Frame {frame_no:04d}: Olympus Wi-Fi client recreate attempt "
                    f"{attempt}/{attempts} failed: {type(exc).__name__}: {exc}"
                    if frame_no is not None
                    else f"Olympus Wi-Fi client recreate attempt {attempt}/{attempts} "
                    f"failed: {type(exc).__name__}: {exc}"
                )
                try_wake_camera_wifi(
                    self.logger,
                    reason=f"client recreate failed after {reason}: {type(exc).__name__}",
                )
                if attempt < attempts:
                    time.sleep(delays[min(attempt - 1, len(delays) - 1)])
        if raise_on_failure:
            raise last_exc
        return False

    def _switch_mode_with_recovery(self, mode, *, frame_no, attempts=3):
        """Enter a camera mode, rebuilding the HTTP client after transient failures."""
        delays = (0.75, 2.0, 5.0)
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                self._switch_mode(mode)
                return
            except Exception as exc:
                last_exc = exc
                self.logger.event(
                    "wifi_mode_switch_recovery",
                    frame_no=frame_no,
                    target_mode=mode,
                    attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._recreate_client(
                    frame_no=frame_no,
                    reason=f"failed switch to {mode} (attempt {attempt})",
                )
                if attempt < attempts:
                    time.sleep(delays[min(attempt - 1, len(delays) - 1)])
        raise last_exc

    def _list_new_jpeg_strict(self, frame_no):
        """Return a genuinely unseen JPEG, or None.

        This differs deliberately from the older listing fallback: it NEVER returns
        the pre-existing newest JPEG when there is no unseen file.  That distinction
        is essential after an ambiguous shutter acknowledgement because accepting an
        old JPEG would falsely claim that the failed exposure happened.
        """
        started = time.monotonic()
        try:
            listing = self.cam.list_images()
        except Exception as exc:
            self._log_command(
                "list_images strict_recovery",
                started,
                ok=False,
                frame_no=frame_no,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        unseen = [f for f in listing if f.file_name not in self.known_names]
        jpgs = [f for f in unseen if f.file_name.lower().endswith((".jpg", ".jpeg"))]
        self._log_command(
            "list_images strict_recovery",
            started,
            frame_no=frame_no,
            listed_files=len(listing),
            unseen_files=len(unseen),
            unseen_jpegs=len(jpgs),
        )
        if not jpgs:
            return None, listing, unseen
        return max(jpgs, key=lambda f: f.date_time), listing, unseen

    def _recover_confirmed_capture_after_discovery_failure(
        self, *, frame_no, predicted, prediction_seed, recovery_reason, physical_attempt
    ):
        """Wait for the camera API to return before deciding the frame is lost.

        Once a shutter command was acknowledged, the safest assumption is that the
        camera may have written the image even if thumbnail/listing calls time out.
        This loop delays duplicate shutter retries while repeatedly trying to prove
        the predicted/new JPEG exists.
        """
        deadline = time.monotonic() + float(CAMERA_RECOVERY_WINDOW_SECONDS)
        poll = max(0.5, float(CAMERA_RECOVERY_POLL_SECONDS))
        attempt = 0
        last_error = None
        self.logger.event(
            "wifi_capture_recovery_window_started",
            frame_no=frame_no,
            physical_attempt=physical_attempt,
            predicted_jpg=predicted,
            window_seconds=float(CAMERA_RECOVERY_WINDOW_SECONDS),
        )
        self.logger.human(
            f"Frame {frame_no:04d}: camera API stalled after shutter; waiting up to "
            f"{float(CAMERA_RECOVERY_WINDOW_SECONDS):g}s to confirm the JPEG before retrying."
        )

        while time.monotonic() < deadline:
            attempt += 1
            if not self._recreate_client(
                frame_no=frame_no,
                reason=f"recovery window attempt {attempt}",
                attempts=1,
                raise_on_failure=False,
            ):
                time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
                continue

            try:
                self._switch_mode("play")
                probe = self._probe_predicted_thumbnail(predicted)
                if probe is not None:
                    self.logger.event(
                        "wifi_discovery_failure_recovered_by_predicted_jpeg",
                        frame_no=frame_no,
                        remote_jpg=predicted,
                        physical_attempt=physical_attempt,
                        recovery_attempt=attempt,
                    )
                    return self._accept_capture(
                        frame_no=frame_no,
                        remote_jpg=predicted,
                        prediction_seed=prediction_seed,
                        probe=probe,
                        discovery_method="predicted",
                        recovery_reason=recovery_reason or "discovery_failure",
                    )

                jpg, listing, unseen = self._list_new_jpeg_strict(frame_no)
                if jpg is not None:
                    self.known_names = {f.file_name for f in listing}
                    self.logger.event(
                        "wifi_discovery_failure_recovered_by_listing",
                        frame_no=frame_no,
                        remote_jpg=jpg.file_name,
                        physical_attempt=physical_attempt,
                        recovery_attempt=attempt,
                    )
                    return self._accept_capture(
                        frame_no=frame_no,
                        remote_jpg=jpg.file_name,
                        prediction_seed=prediction_seed,
                        discovery_method="listing_fallback",
                        new_files=[f.file_name for f in unseen] or [jpg.file_name],
                        recovery_reason=recovery_reason or "discovery_failure",
                    )

                self.logger.event(
                    "wifi_recovery_camera_reachable_no_new_jpeg",
                    frame_no=frame_no,
                    physical_attempt=physical_attempt,
                    recovery_attempt=attempt,
                    predicted_jpg=predicted,
                )
                return None
            except Exception as exc:
                last_error = exc
                self.logger.event(
                    "wifi_capture_recovery_window_attempt_failed",
                    frame_no=frame_no,
                    physical_attempt=physical_attempt,
                    recovery_attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}",
                )
                time.sleep(min(poll, max(0.0, deadline - time.monotonic())))

        self.logger.event(
            "wifi_capture_recovery_window_exhausted",
            frame_no=frame_no,
            physical_attempt=physical_attempt,
            predicted_jpg=predicted,
            window_seconds=float(CAMERA_RECOVERY_WINDOW_SECONDS),
            last_error=(f"{type(last_error).__name__}: {last_error}" if last_error else None),
        )
        msg = (
            f"Frame {frame_no}: camera API stayed unreachable for "
            f"{float(CAMERA_RECOVERY_WINDOW_SECONDS):g}s after shutter; "
            "could not safely confirm the JPEG"
        )
        raise RuntimeError(msg) from last_error

    def _accept_capture(self, *, frame_no, remote_jpg, prediction_seed, probe=None,
                        discovery_method="predicted", new_files=None,
                        recovery_reason=None):
        """Commit one confirmed camera JPEG to prediction/cache state."""
        self.last_remote_jpg = remote_jpg
        self.known_names.add(remote_jpg)
        if probe is not None:
            self._thumbnail_cache[remote_jpg] = bytes(probe)
        if recovery_reason:
            self.logger.event(
                "wifi_capture_recovered",
                frame_no=frame_no,
                remote_jpg=remote_jpg,
                recovery_reason=recovery_reason,
                discovery_method=discovery_method,
            )
            self.logger.human(
                f"Frame {frame_no:04d}: recovery confirmed {Path(remote_jpg).name}; continuing run."
            )
        self.logger.event(
            "wifi_capture_predicted" if discovery_method == "predicted" else "wifi_prediction_recovered_by_listing",
            frame_no=frame_no,
            remote_jpg=remote_jpg,
            predicted_from=prediction_seed,
            thumbnail_bytes=len(probe) if probe is not None else None,
        )
        return CaptureResult(
            remote_jpg=remote_jpg,
            remote_orf=None,
            new_files=list(new_files or [remote_jpg]),
            discovery_method=discovery_method,
        )

    def _fire_shutter(self, frame_no):
        """Fire one still and report acknowledgement state instead of killing the run.

        Any exception from the shutter-mode transition, press, or release is treated
        as *ambiguous*: the camera may have received the command before the TCP reset.
        capture() therefore reconnects and looks for the predicted/new JPEG BEFORE it
        ever retries the shutter.  This prevents duplicate physical exposures.

        Returns one of: "ok", "press_ack_ambiguous", "release_ack_ambiguous".
        """
        try:
            self._switch_mode("shutter")
        except Exception as exc:
            self.logger.event(
                "wifi_shutter_mode_ack_ambiguous",
                frame_no=frame_no,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.logger.human(
                f"Frame {frame_no:04d}: shutter-mode command failed; will reconnect and "
                "check the SD state before any shutter retry."
            )
            return "press_ack_ambiguous"

        press_t0 = time.monotonic()
        try:
            self.cam.send_command("exec_shutter", com="1st2ndpush")
            time.sleep(SHUTTER_HOLD_SECONDS)
            self._log_command(f"shutter_press frame={frame_no}", press_t0)
        except Exception as exc:
            self._log_command(
                f"shutter_press frame={frame_no}", press_t0, ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.logger.event(
                "wifi_shutter_press_ack_ambiguous",
                frame_no=frame_no,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.logger.human(
                f"Frame {frame_no:04d}: shutter press acknowledgement was lost; "
                "will reconnect and probe for the JPEG before re-firing."
            )
            return "press_ack_ambiguous"

        release_t0 = time.monotonic()
        try:
            self.cam.send_command("exec_shutter", com="2nd1strelease")
            self._log_command(f"shutter_release frame={frame_no}", release_t0)
            return "ok"
        except Exception as exc:
            self._log_command(
                f"shutter_release frame={frame_no}", release_t0, ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.logger.event(
                "wifi_shutter_release_ack_ambiguous",
                frame_no=frame_no,
                error=f"{type(exc).__name__}: {exc}",
            )
            self.logger.human(
                f"Frame {frame_no:04d}: shutter release acknowledgement was lost; "
                "will reconnect and probe predicted JPEG before considering any retry."
            )
            return "release_ack_ambiguous"

    def capture(self, frame_no):
        """Capture one frame with bounded recovery from transient Olympus resets.

        Normal fast path:
          shutter -> PLAY -> predicted thumbnail -> success

        Recovery path:
          ambiguous/reset -> fresh Olympus client -> PLAY -> predicted thumbnail
          -> strict unseen-file listing -> only if neither proves an exposure occurred,
          retry the physical shutter.  Three physical attempts are allowed, with
          escalating 0.75/2/5 s recovery delays.  A run becomes fatal only after the
          bounded recovery budget is exhausted.
        """
        if not self.last_remote_jpg:
            raise RuntimeError("Predictive capture has no startup filename seed")

        prediction_seed = self.last_remote_jpg
        predicted = self._predict_next_jpeg(prediction_seed)
        retry_delays = (0.75, 2.0, 5.0)
        max_physical_attempts = 3
        last_error = None

        for physical_attempt in range(1, max_physical_attempts + 1):
            ack_state = self._fire_shutter(frame_no)
            recovery_reason = None if ack_state == "ok" else ack_state

            # A reset leaves the old HTTP client suspect.  Recreate it before doing
            # anything else, but do not alter filename prediction or exposure state.
            if ack_state != "ok":
                self._recreate_client(
                    frame_no=frame_no,
                    reason=f"{ack_state} on physical attempt {physical_attempt}",
                )

            time.sleep(CARD_WRITE_WAIT_SECONDS)

            # If the shutter definitely or possibly fired, our first duty is to
            # determine whether a JPEG exists.  Never re-fire until that is disproven.
            try:
                self._switch_mode_with_recovery("play", frame_no=frame_no)
            except Exception as exc:
                last_error = exc
                self.logger.error(
                    "wifi_capture_recovery_play_mode",
                    exc,
                    frame_no=frame_no,
                    physical_attempt=physical_attempt,
                )
            else:
                probe = self._probe_predicted_thumbnail(predicted)
                if probe is not None:
                    if recovery_reason:
                        self.logger.event(
                            "wifi_shutter_ack_recovered_by_predicted_jpeg",
                            frame_no=frame_no,
                            remote_jpg=predicted,
                            ack_state=ack_state,
                            physical_attempt=physical_attempt,
                        )
                    return self._accept_capture(
                        frame_no=frame_no,
                        remote_jpg=predicted,
                        prediction_seed=prediction_seed,
                        probe=probe,
                        discovery_method="predicted",
                        recovery_reason=recovery_reason,
                    )

                # Prediction can fail legitimately at an Olympus filename/folder
                # rollover.  A strict listing can prove that a different new JPEG
                # exists without ever confusing the previous frame for this one.
                try:
                    jpg, listing, unseen = self._list_new_jpeg_strict(frame_no)
                except Exception as exc:
                    last_error = exc
                    self.logger.event(
                        "wifi_strict_listing_failed",
                        frame_no=frame_no,
                        physical_attempt=physical_attempt,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    recovered = self._recover_confirmed_capture_after_discovery_failure(
                        frame_no=frame_no,
                        predicted=predicted,
                        prediction_seed=prediction_seed,
                        recovery_reason=recovery_reason or "strict_listing_failure",
                        physical_attempt=physical_attempt,
                    )
                    if recovered is not None:
                        return recovered
                else:
                    if jpg is not None:
                        self.known_names = {f.file_name for f in listing}
                        return self._accept_capture(
                            frame_no=frame_no,
                            remote_jpg=jpg.file_name,
                            prediction_seed=prediction_seed,
                            discovery_method="listing_fallback",
                            new_files=[f.file_name for f in unseen] or [jpg.file_name],
                            recovery_reason=recovery_reason or "prediction_miss",
                        )

            if physical_attempt >= max_physical_attempts:
                break

            delay = retry_delays[min(physical_attempt - 1, len(retry_delays) - 1)]
            self.logger.event(
                "wifi_capture_retry_scheduled",
                frame_no=frame_no,
                physical_attempt=physical_attempt,
                next_physical_attempt=physical_attempt + 1,
                delay_seconds=delay,
                predicted_jpg=predicted,
            )
            self.logger.human(
                f"Frame {frame_no:04d}: no new JPEG could be confirmed after attempt "
                f"{physical_attempt}; waiting {delay:g}s, then retrying the shutter."
            )
            time.sleep(delay)
            self._recreate_client(
                frame_no=frame_no,
                reason=f"preparing physical shutter retry {physical_attempt + 1}",
            )

        msg = (
            f"Frame {frame_no}: Olympus capture recovery exhausted after "
            f"{max_physical_attempts} physical attempts; no new JPEG could be confirmed"
        )
        self.logger.event(
            "wifi_capture_recovery_exhausted",
            frame_no=frame_no,
            physical_attempts=max_physical_attempts,
            predicted_jpg=predicted,
            last_error=(f"{type(last_error).__name__}: {last_error}" if last_error else None),
        )
        raise RuntimeError(msg) from last_error

    def download_thumbnail(self, remote_jpg: str, local_path=None):
        """Return thumbnail bytes, using the capture probe cache when available."""
        self._switch_mode("play")
        if remote_jpg in self._thumbnail_cache:
            data = self._thumbnail_cache.pop(remote_jpg)
            self.logger.event(
                "thumbnail_cache_hit",
                remote_path=remote_jpg,
                bytes=len(data),
            )
            return data

        t0 = time.monotonic()
        data = self.cam.download_thumbnail(remote_jpg)
        self._log_command(
            f"download_thumbnail {remote_jpg}",
            t0,
            bytes=len(data) if isinstance(data, (bytes, bytearray)) else None,
        )
        return data

    def download_jpeg(self, remote_jpg: str, local_path: Path):
        self._switch_mode("play")
        t0 = time.monotonic()
        data = self.cam.download_image(remote_jpg)
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        self._log_command(
            f"download_image {remote_jpg}", t0,
            bytes=len(data), local_path=str(local_path),
        )
        return local_path

    def power_down(self):
        # exec_pwoff exists in the camera command list, but we have not yet
        # physically tested it on this Wi-Fi path. V5 intentionally leaves the
        # camera powered rather than guessing at end-of-run behavior.
        self.logger.human("Wi-Fi V5: camera power-down skipped (not yet physically tested).")
