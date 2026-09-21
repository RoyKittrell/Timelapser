import unittest
import json
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from v5_overlay_context import air_quality_for_run, solar_markers


def rows(start, end):
    return [{"frame": "1", "time": start}, {"frame": "2", "time": end}]


class OverlayContextTests(unittest.TestCase):
    def test_solar_markers_follow_capture_time(self):
        sunset = solar_markers(rows("2026-09-17T18:26:00", "2026-09-17T20:21:00"))
        sunrise = solar_markers(rows("2026-09-18T06:23:00", "2026-09-18T07:54:00"))
        midday = solar_markers(rows("2026-09-17T12:00:00", "2026-09-17T13:00:00"))
        dusk_slice = solar_markers(rows("2026-09-17T19:20:00", "2026-09-17T19:36:00"))
        self.assertEqual([kind for _, kind in sunset], ["golden", "blue", "night"])
        self.assertEqual([kind for _, kind in sunrise], ["night", "blue", "golden"])
        self.assertEqual(midday, [])
        self.assertEqual([kind for _, kind in dusk_slice], ["blue", "night"])

    def test_air_quality_is_cached_near_capture_midpoint(self):
        payload = {"hourly": {"time": ["2026-09-21T18:00", "2026-09-21T19:00", "2026-09-21T20:00"],
                              "us_aqi": [60, 42, 55]}}
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with patch("v5_overlay_context.urlopen", return_value=BytesIO(json.dumps(payload).encode())):
                first = air_quality_for_run(run_dir, rows("2026-09-21T18:24:00", "2026-09-21T20:19:00"))
            with patch("v5_overlay_context.urlopen", side_effect=OSError("offline")):
                second = air_quality_for_run(run_dir, rows("2026-09-21T18:24:00", "2026-09-21T20:19:00"))
            self.assertEqual(first, second)
            self.assertEqual(first["us_aqi"], 42)

    def test_air_quality_outage_does_not_block_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("v5_overlay_context.urlopen", side_effect=OSError("offline")), \
                 patch("v5_overlay_context.time.sleep"):
                self.assertIsNone(air_quality_for_run(Path(temporary), rows("2026-09-21T18:24:00", "2026-09-21T20:19:00")))


if __name__ == "__main__":
    unittest.main()
