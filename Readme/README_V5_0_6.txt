TIMELAPSER V5.0.6 — Olympus Wi-Fi Reset Recovery

Replace these files in your timelapser_v5 project directory:
  timelapser_v5.py
  v5_camera.py
  v5_commander.py   (unchanged from the supplied V5.0.5 bundle; included for completeness)

Why this version exists
-----------------------
A real sunset run on 10 Sep 2026 reached 21 completed frames, then the Olympus
Wi-Fi peer reset the TCP connection during frame 22's exec_shutter 1st2ndpush.
V5.0.5 logged the shutter error but let it propagate to main as FATAL, ending the run.

V5.0.6 changes
--------------
1. Connection resets during shutter control are recoverable
   - shutter-mode, shutter-press, and shutter-release failures are treated as
     ambiguous acknowledgements rather than immediately fatal errors.
   - the possibly poisoned Olympus HTTP client is discarded and recreated.

2. Never blindly re-fire after a lost acknowledgement
   - after any ambiguous shutter command, V5 first reconnects in PLAY mode and
     probes the predicted JPEG.
   - if the predicted JPEG exists, that proves the exposure happened and V5
     continues without firing another physical exposure.

3. Strict SD-card recovery check
   - if the predicted JPEG is unavailable, V5 performs an authoritative SD listing.
   - during ambiguity recovery, ONLY genuinely unseen JPEGs count as proof of a
     new exposure. The previous frame can no longer be mistaken for the failed frame.
   - this also handles Olympus filename/folder rollover safely.

4. Bounded physical shutter retries
   - if neither the predicted JPEG nor a genuinely new listed JPEG exists, V5
     retries the physical shutter.
   - up to 3 physical attempts are allowed.
   - recovery waits escalate approximately 0.75 s -> 2 s before subsequent attempts.
   - only after the recovery budget is exhausted does capture become fatal.

5. Mode-switch recovery
   - PLAY-mode recovery itself gets up to 3 attempts, recreating the Olympus
     client after failures.

6. New forensic events
   - wifi_client_recreated
   - wifi_shutter_mode_ack_ambiguous
   - wifi_shutter_press_ack_ambiguous
   - wifi_shutter_release_ack_ambiguous
   - wifi_shutter_ack_recovered_by_predicted_jpeg
   - wifi_capture_recovered
   - wifi_capture_retry_scheduled
   - wifi_mode_switch_recovery
   - wifi_strict_listing_failed
   - wifi_capture_recovery_exhausted

Safety principle
----------------
A network error does NOT mean the camera failed to expose. V5.0.6 therefore follows:

  command error
      -> reconnect
      -> check predicted JPEG
      -> check genuinely unseen SD JPEG
      -> only then re-fire

This is specifically designed to avoid duplicate physical frames after an Olympus
HTTP acknowledgement is lost.

Validation performed
--------------------
- Python syntax/bytecode compilation passed.
- Simulated press-reset with NO exposure: V5 reconnected, proved no JPEG existed,
  retried once, and successfully returned frame 22.
- Simulated press-reset AFTER the exposure occurred: V5 reconnected, found the
  predicted JPEG, accepted frame 22, and did NOT fire a second exposure.

Recommended first test
----------------------
python3 timelapser_v5.py \
  --mode sunset \
  --interval 5 \
  --max-frames 100 \
  --transfer-mode thumbnail

A transient Olympus reset may make one interval late, but should no longer end the
whole timelapse unless all bounded recovery attempts fail.
