import json
import math
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from solar_diagrams import analemma_phase, analemma_point, horizon_azimuth, run_direction, season_dates, solar_terms


class SolarDiagramsTests(unittest.TestCase):
    def test_horizon_azimuth(self):
        equinox = date(2026, 9, 23)
        sunrise = horizon_azimuth(equinox, "sunrise")
        sunset = horizon_azimuth(equinox, "sunset")
        self.assertAlmostEqual(sunrise + sunset, 360)
        self.assertLess(abs(sunset - 270), 2)
        self.assertGreater(horizon_azimuth(date(2026, 6, 21), "sunset"), 290)
        self.assertLess(horizon_azimuth(date(2026, 12, 22), "sunset"), 250)

    def test_analemma_terms_change_through_year(self):
        june = solar_terms(date(2026, 6, 21))
        december = solar_terms(date(2026, 12, 22))
        self.assertGreater(june[1], 0)
        self.assertLess(december[1], 0)
        self.assertNotEqual(june[0], december[0])

    def test_schematic_analemma_has_symmetric_unequal_loops(self):
        for phase in (0.2, 0.5, 1.0):
            left = analemma_point(math.pi - phase)
            right = analemma_point(phase)
            self.assertAlmostEqual(left[0], -right[0])
            self.assertAlmostEqual(left[1], right[1])
        top = max(abs(analemma_point(i * math.pi / 100)[0]) for i in range(101))
        bottom = max(abs(analemma_point(math.pi + i * math.pi / 100)[0]) for i in range(101))
        self.assertGreater(bottom, top * 1.5)
        self.assertGreater(analemma_point(3 * math.pi / 2)[1],
                           abs(analemma_point(math.pi / 2)[1]) * 1.5)

    def test_dot_hits_event_points(self):
        dates = season_dates(2026)
        self.assertAlmostEqual(analemma_phase(dates["March equinox"], dates), 0)
        self.assertAlmostEqual(analemma_phase(dates["June solstice"], dates), math.pi / 2)
        self.assertAlmostEqual(analemma_phase(dates["September equinox"], dates), math.pi)
        self.assertAlmostEqual(analemma_phase(dates["December solstice"], dates), 3 * math.pi / 2)

    def test_usno_dates_are_local_and_cached(self):
        payload = {"data": [
            {"year": 2026, "month": 3, "day": 20, "time": "14:46", "phenom": "Equinox"},
            {"year": 2026, "month": 6, "day": 21, "time": "08:24", "phenom": "Solstice"},
            {"year": 2026, "month": 9, "day": 23, "time": "00:05", "phenom": "Equinox"},
            {"year": 2026, "month": 12, "day": 21, "time": "20:50", "phenom": "Solstice"},
        ]}
        with tempfile.TemporaryDirectory() as temporary:
            with patch("solar_diagrams.urlopen", return_value=BytesIO(json.dumps(payload).encode())):
                first = season_dates(2026, Path(temporary))
            with patch("solar_diagrams.urlopen", side_effect=OSError("offline")):
                second = season_dates(2026, Path(temporary))
        self.assertEqual(first, second)
        self.assertEqual(first["June solstice"], date(2026, 6, 21))
        self.assertEqual(first["December solstice"], date(2026, 12, 22))

    def test_general_run_has_no_horizon_marker(self):
        self.assertEqual(run_direction([{"time": "2026-09-21T18:24:00"}]), "sunset")
        self.assertEqual(run_direction([{"time": "2026-09-22T06:23:00"}]), "sunrise")
        self.assertIsNone(run_direction([{"time": "2026-09-21T13:00:00"}]))

    def test_verified_2026_dates_work_offline(self):
        with patch("solar_diagrams.urlopen", side_effect=OSError("offline")):
            actual = season_dates(2026)
        self.assertEqual(actual["September equinox"], date(2026, 9, 23))
        self.assertEqual(actual["December solstice"], date(2026, 12, 22))


if __name__ == "__main__":
    unittest.main()
