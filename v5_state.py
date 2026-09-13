from dataclasses import dataclass, field, asdict
from threading import Lock
from typing import Optional
import time


@dataclass
class TimelapseState:
    mode: str
    run_dir: str
    physical_frames_requested: int = 0
    downloaded_frames: int = 0
    next_frame_no: int = 1

    iso: int = 200
    aperture: float = 6.3
    shutter_seconds: float = 1 / 125

    last_remote_jpg: Optional[str] = None
    last_remote_orf: Optional[str] = None
    last_local_jpeg: Optional[str] = None
    last_preview_jpeg: Optional[str] = None
    last_image_metrics: dict = field(default_factory=dict)
    scene_trend: dict = field(default_factory=dict)
    holy_grail_shadow: dict = field(default_factory=dict)

    ai_last_note: str = ""
    ai_last_decision_at: Optional[float] = None
    ai_last_review_median: Optional[float] = None
    ai_last_applied_delta_ev: Optional[float] = None

    capture_paused_until: float = 0.0
    abort_requested: bool = False
    abort_reason: str = ""

    def snapshot(self):
        d = asdict(self)
        d["now_monotonic"] = time.monotonic()
        return d


class SharedState:
    def __init__(self, state: TimelapseState):
        self._state = state
        self._lock = Lock()

    def snapshot(self):
        with self._lock:
            return self._state.snapshot()

    def update(self, fn):
        with self._lock:
            fn(self._state)

    def get(self):
        return self._state
