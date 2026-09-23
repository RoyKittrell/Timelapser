"""
User-editable overlay configuration for Timelapser Renderer V3.1.

All positions/sizes are expressed as fractions of the OUTPUT VIDEO dimensions unless
noted otherwise. This makes the same layout scale sensibly between 1080x1920 and
2160x3840.

Safe-area guidance for Instagram Reels:
- Keep important text away from the extreme top/bottom/right UI areas.
- The defaults below deliberately leave generous margins.

Text is rendered white with a thin black outline for readability over any scene.
"""

# -----------------------------------------------------------------------------
# GLOBAL SAFE AREA / MARGINS
# -----------------------------------------------------------------------------
# Fractions of video width/height. Example: 0.06 = 6% of the dimension.
MARGIN_LEFT = 0.060
MARGIN_RIGHT = 0.060
MARGIN_TOP = 0.075
MARGIN_BOTTOM = 0.105

# Hard constraint: no overlay element is allowed outside the safe rectangle created
# by these margins. The renderer clamps/rejects geometry that would exceed it.
ENFORCE_SAFE_AREA = True

# -----------------------------------------------------------------------------
# TYPOGRAPHY
# -----------------------------------------------------------------------------
# A standard font normally present on Raspberry Pi OS / Debian.
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Sizes are fractions of VIDEO HEIGHT, so they scale cleanly with resolution.
TEXT_SIZE_SMALL = 0.020
TEXT_SIZE_MEDIUM = 0.027
TEXT_SIZE_LARGE = 0.036
TEXT_SIZE_TITLE = 0.030

TEXT_FILL = (255, 255, 255, 255)
TEXT_STROKE_FILL = (0, 0, 0, 255)
# Fraction of video height. ~2 px at 1080x1920, ~4 px at 2160x3840.
TEXT_STROKE_WIDTH = 0.0012

# -----------------------------------------------------------------------------
# GRAPH STYLE
# -----------------------------------------------------------------------------
# Brightness graph box placement. Coordinates are relative to the SAFE AREA.
# x/y = top-left; width/height = dimensions.
# Director graph: compact lower-third telemetry chart.
DIRECTOR_BRIGHTNESS_GRAPH = {
    "x": 0.00,
    "y": 0.73,
    "width": 1.00,
    "height": 0.25,
}

# Brightness-only graph: deliberately dominates most of the safe frame.
BRIGHTNESS_ONLY_GRAPH = {
    "x": 0.00,
    "y": 0.12,
    "width": 1.00,
    "height": 0.76,
}

# Backwards-compatible alias for custom code.
BRIGHTNESS_GRAPH = DIRECTOR_BRIGHTNESS_GRAPH

# Graph appearance.
GRAPH_LINE_WIDTH = 0.0030       # fraction of video width
GRAPH_EDGE_WIDTH = 0.0015       # black edge behind white trace
GRAPH_AXIS_WIDTH = 0.0015
GRAPH_GRID_WIDTH = 0.0008
GRAPH_PANEL_ALPHA = 58          # 0 = no panel, 255 = opaque black
GRAPH_GRID_ALPHA = 95
GRAPH_AXIS_ALPHA = 190
GRAPH_POINT_RADIUS = 0.0055     # fraction of video width
GRAPH_Y_MIN = 0.0
GRAPH_Y_MAX = 1.0

# If True, the graph reveals only samples up to the current frame. Future brightness
# samples are intentionally hidden — this is the "live graph" behaviour.
GRAPH_REVEAL_LIVE = True

# Number of horizontal guide lines, including min/max. 5 gives 0, .25, .5, .75, 1.
GRAPH_HORIZONTAL_GUIDES = 5

# Show basic axis labels on brightness graphs. Brightness is unitless linear
# median luminance measured from the JPEG frames being rendered.
GRAPH_SHOW_AXIS_LABELS = True
GRAPH_Y_AXIS_LABEL = "Brightness (linear median, 0-1)"

# Date label rendered as e.g. "12 Jan 2026".
OVERLAY_SHOW_DATE = True

# -----------------------------------------------------------------------------
# BRIGHTNESS-ONLY VIDEO
# -----------------------------------------------------------------------------
BRIGHTNESS_SHOW_TITLE = True
BRIGHTNESS_TITLE = "SCENE BRIGHTNESS"
BRIGHTNESS_SHOW_CURRENT_VALUE = True
BRIGHTNESS_SHOW_TIME = True
BRIGHTNESS_SHOW_FRAME = False

# -----------------------------------------------------------------------------
# AI DIRECTOR VIDEO
# -----------------------------------------------------------------------------
DIRECTOR_TITLE = "@timelapser.188"
DIRECTOR_SHOW_FRAME = True
DIRECTOR_SHOW_TIME = True
DIRECTOR_SHOW_EXPOSURE = True
DIRECTOR_SHOW_SCENE_TREND = False
DIRECTOR_SHOW_BRIGHTNESS_GRAPH = True
DIRECTOR_SHOW_CURRENT_BRIGHTNESS = True

# Top information block, relative to safe area.
DIRECTOR_INFO_X = 0.00
DIRECTOR_INFO_Y = 0.00
# Exposure text shrinks as needed so it cannot enter the analemma area.
DIRECTOR_EXPOSURE_RIGHT = 0.79
DIRECTOR_EXPOSURE_MIN_SIZE = 0.024

# Spacing between Director text rows, as fraction of video height.
DIRECTOR_LINE_SPACING = 0.008

# -----------------------------------------------------------------------------
# OUTPUTS
# -----------------------------------------------------------------------------
# Running render_timelapse.py with no --outputs argument makes all of these.
# clean      = normal Instagram timelapse, no overlay
# brightness = live brightness graph overlay
# director   = camera/scene telemetry + live brightness graph
DEFAULT_OUTPUTS = ["clean", "brightness", "director"]

# Overlay PNGs are temporary by default. Set True if you want to inspect/edit them.
KEEP_OVERLAY_FRAMES = False
