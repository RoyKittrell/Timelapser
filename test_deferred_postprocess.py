import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from timelapser_scheduler import (
    Mission,
    deferred_requests,
    nearby_capture,
    postprocess_defer_window,
    reconcile_interrupted_deferred_requests,
)


def mission(key: str, start: datetime, end: datetime) -> Mission:
    return Mission(
        key=key, date="2026-09-28", event="general", event_time="21:00",
        enabled=True, mode="general", start_offset_minutes=0,
        end_offset_minutes=60, interval=5, iso=200, aperture=6.3,
        shutter=0.01, transfer_mode="thumbnail", max_frames=None,
        download_fullres_after=True, render_after=True,
        copy_videos_to_mac_after=False, mac_video_dest="", notes="",
        event_dt=start, start_dt=start, end_dt=end,
        preflight_dt=start - timedelta(minutes=10),
    )


class DeferredPostprocessTests(unittest.TestCase):
    def test_three_hour_buffer_is_configurable(self):
        self.assertEqual(postprocess_defer_window(SimpleNamespace()), timedelta(hours=3))
        self.assertEqual(
            postprocess_defer_window(SimpleNamespace(POSTPROCESS_DEFER_WINDOW_HOURS=1.5)),
            timedelta(hours=1.5),
        )

    def test_nearby_mission_blocks_but_later_mission_does_not(self):
        now = datetime.now().astimezone()
        close = mission("close", now + timedelta(hours=2), now + timedelta(hours=3))
        later = mission("later", now + timedelta(hours=4), now + timedelta(hours=5))
        self.assertEqual(nearby_capture([close], {"events": {}}, now, timedelta(hours=3)), close)
        self.assertIsNone(nearby_capture([later], {"events": {}}, now, timedelta(hours=3)))

    def test_completed_mission_does_not_block(self):
        now = datetime.now().astimezone()
        close = mission("close", now + timedelta(hours=1), now + timedelta(hours=2))
        state = {"events": {"close": {"status": "complete"}}}
        self.assertIsNone(nearby_capture([close], state, now, timedelta(hours=3)))

    def test_requests_are_returned_oldest_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, hours in (("new", 2), ("old", 1)):
                folder = root / name
                folder.mkdir()
                payload = {
                    "status": "pending",
                    "created_at": (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(),
                    "run_dir": str(folder),
                }
                (folder / "postprocess_deferred.json").write_text(json.dumps(payload))
            self.assertEqual([item[1].parent.name for item in deferred_requests(root)], ["old", "new"])

    def test_interrupted_request_returns_to_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "run"
            folder.mkdir()
            path = folder / "postprocess_deferred.json"
            path.write_text(json.dumps({
                "status": "running",
                "created_at": datetime.now().astimezone().isoformat(),
                "run_dir": str(folder),
            }))
            self.assertEqual(reconcile_interrupted_deferred_requests(root), 1)
            recovered = json.loads(path.read_text())
            self.assertEqual(recovered["status"], "pending")
            self.assertEqual(recovered["recovery_count"], 1)


if __name__ == "__main__":
    unittest.main()
