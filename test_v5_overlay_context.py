import unittest

from v5_overlay_context import solar_markers


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


if __name__ == "__main__":
    unittest.main()
