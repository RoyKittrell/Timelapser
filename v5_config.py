from pathlib import Path

MODE_DEFAULT = "sunset"
CAPTURE_INTERVAL_SECONDS = 30.0
MAX_RUN_MINUTES = 150

# RAW+JPEG must be selected on the camera body. RAW stays on SD; JPEG is
# downloaded over the dedicated camera Wi-Fi link for metering/AI.
CARD_WRITE_WAIT_SECONDS = 1.0
FILE_DISCOVERY_TIMEOUT_SECONDS = 6.0
FILE_DISCOVERY_POLL_SECONDS = 0.35
SHUTTER_HOLD_SECONDS = 0.50
MODE_SETTLE_SECONDS = 0.10

# Bound Olympus HTTP stalls. The timeout tuple is (connect timeout, read timeout).
# Shutter timeouts are treated as ambiguous by v5_camera.py recovery logic.
OLYMPUS_CGI_TIMEOUT_SECONDS = (2.0, 8.0)
OLYMPUS_FULL_IMAGE_TIMEOUT_SECONDS = (2.0, 45.0)

# If the camera accepts a shutter command but its HTTP API drops during file
# discovery, wait for the Wi-Fi/API to recover before risking a duplicate frame.
CAMERA_RECOVERY_WINDOW_SECONDS = 180.0
CAMERA_RECOVERY_POLL_SECONDS = 5.0

# E-M5 III Bluetooth can wake the camera's Wi-Fi/OI.Share endpoint.  Keep this
# optional: normal Wi-Fi capture remains the primary transport, and these values
# are only used after the Olympus HTTP API is already failing.
ENABLE_BLUETOOTH_WIFI_WAKE = True
CAMERA_WIFI_INTERFACE = "wlan1"
CAMERA_WIFI_CONNECTION_NAME = "E-M5MKIII-P-BJ8A00203"
CAMERA_BLUETOOTH_ADDRESS = "10:98:C3:F2:18:17"
CAMERA_BLUETOOTH_NAME = "BJ8A00203"
CAMERA_WIFI_WAKE_TIMEOUT_SECONDS = 45.0
CAMERA_WIFI_WAKE_POLL_SECONDS = 2.0

# Director-requested ad-hoc runs may be longer than the historical 150 minute
# safety ceiling. Keep a conservative upper bound for typo protection.
DIRECTOR_MAX_DURATION_SECONDS = 6 * 60 * 60

MIN_ISO = 200
MAX_ISO = 1600

# Lens-aware V5 control profile. The M.Zuiko 75-300mm is physically
# f/4.8-6.7, but the Olympus Wi-Fi API only accepts standard body aperture
# values and rejects f/4.8. Use the nearest commandable safe floor, f/5.0.
LENS_PROFILE_NAME = "M.Zuiko 75-300mm f/4.8-6.7"
LENS_MIN_APERTURE = 4.8
LENS_MAX_APERTURE = 6.7
CONTROL_MIN_APERTURE = 5.0
MIN_APERTURE = CONTROL_MIN_APERTURE
MAX_APERTURE = 8.0
MIN_SHUTTER_SECONDS = 1 / 1000
MAX_SHUTTER_SECONDS = 1.0
PREFERRED_MAX_SHUTTER_SECONDS = 1 / 4

SUNSET_BASELINE = {"iso": 200, "aperture": 6.3, "shutter_seconds": 1 / 125}
SUNRISE_BASELINE = {"iso": 200, "aperture": 5.0, "shutter_seconds": 1.0}
GENERAL_BASELINE = {"iso": 200, "aperture": 6.3, "shutter_seconds": 1 / 125}

ENABLE_AI_COMMANDER = True
AI_MODEL = "gpt-5.6-luna"
AI_IMAGE_DETAIL = "low"
AI_TIMEOUT_SECONDS = 18.0
AI_REVIEW_EVERY_N_DOWNLOADED_FRAMES = 5
MAX_AI_EXPOSURE_STEP_EV = 0.25
AI_EXPOSURE_DEADBAND_EV = 0.15
AI_EXPOSURE_REVERSAL_DEADBAND_EV = 0.35

# Shadow-mode Holy Grail controller. This does not control the camera yet; it
# logs exposure-normalized recommendations so we can compare them with real runs.
HOLY_GRAIL_SHADOW_ENABLED = True
HOLY_GRAIL_TARGET_P50_DAY = 110
HOLY_GRAIL_TARGET_P50_NIGHT = 80
HOLY_GRAIL_TRACKER_WINDOW = 20
HOLY_GRAIL_TRACKER_WARMUP = 5
HOLY_GRAIL_RECENCY_DECAY = 0.92
HOLY_GRAIL_ANOMALY_THRESHOLD_EV = 1.0

ENV_FILE = Path("/home/roy/Timelapser Sept2026/env/.env")
CONTROL_DIR = Path("/home/roy/Timelapser Sept2026/timelapser_v5/control")
STOP_REQUEST_FILE = CONTROL_DIR / "stop_requested.json"
DIRECTOR_CONTROL_LOG = CONTROL_DIR / "director_control.log"
OUTPUT_FPS = 60
MAX_VIDEO_SECONDS = 59
MAX_FRAMES = OUTPUT_FPS * MAX_VIDEO_SECONDS

# Olympus Wi-Fi endpoint used by the library. Routing should naturally go via
# wlan1 because the camera network is 192.168.0.x while wlan0 remains online.
CAMERA_IP = "192.168.0.10"
