import datetime as dt
import unittest

import schedule
import timelapser_scheduler


class ScheduleTests(unittest.TestCase):
    def test_solar_capture_windows_include_thirty_extra_minutes_each_side(self):
        self.assertEqual(schedule.DEFAULTS["sunrise"]["start_offset_minutes"], -70)
        self.assertEqual(schedule.DEFAULTS["sunrise"]["end_offset_minutes"], 80)
        self.assertEqual(schedule.DEFAULTS["sunset"]["start_offset_minutes"], -75)
        self.assertEqual(schedule.DEFAULTS["sunset"]["end_offset_minutes"], 100)

    def test_known_kuala_lumpur_solar_times(self):
        day = dt.date(2026, 10, 14)
        self.assertEqual(schedule._solar_event_time(day, True), "06:58")
        self.assertEqual(schedule._solar_event_time(day, False), "19:01")

    def test_schedule_has_a_rolling_horizon(self):
        final_day = dt.date.today() + dt.timedelta(days=schedule.AUTO_SCHEDULE_DAYS_AHEAD)
        keys = {(entry["date"], entry["event"]) for entry in schedule.SCHEDULE}
        self.assertIn((final_day.isoformat(), "sunrise"), keys)
        self.assertIn((final_day.isoformat(), "sunset"), keys)

    def test_explicit_rows_are_not_replaced(self):
        entries = [
            entry
            for entry in schedule.SCHEDULE
            if entry["date"] == "2026-10-13" and entry["event"] == "sunrise"
        ]
        self.assertEqual(len(entries), 1)
        self.assertNotEqual(entries[0].get("notes"), "Auto-generated offline solar schedule")

    def test_new_years_eve_run_crosses_midnight(self):
        missions = timelapser_scheduler.build_missions(schedule)
        mission = next(item for item in missions if item.key == "2026-12-31_general_2230")
        self.assertEqual(mission.start_dt.isoformat(), "2026-12-31T22:30:00+08:00")
        self.assertEqual(mission.end_dt.isoformat(), "2027-01-01T02:00:00+08:00")
        self.assertEqual(mission.max_frames, 2520)


if __name__ == "__main__":
    unittest.main()
