import json
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from solar_diagrams import analemma_crossing, horizon_azimuth, run_direction, season_dates, solar_terms


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

    def test_analemma_crossing_is_computed_from_both_paths(self):
        eqtime, decl = analemma_crossing(2026)
        self.assertLess(abs(eqtime), 10)
        self.assertLess(abs(decl), 0.3)

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
