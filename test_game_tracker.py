import unittest
from datetime import date

import main


class GameUsageTrackerTests(unittest.TestCase):
    def test_daily_and_weekly_usage_are_calculated(self):
        tracker = main.GameUsageTracker(db_path=":memory:")
        tracker.set_limits(daily_minutes=120, weekly_minutes=300)

        tracker.log_session("2026-11-06", 45)
        tracker.log_session("2026-11-06", 30)
        tracker.log_session("2026-11-07", 50)

        self.assertEqual(tracker.get_daily_usage("2026-11-06"), 75)
        self.assertEqual(tracker.get_week_usage("2026-11-06"), 125)
        self.assertFalse(tracker.is_daily_locked("2026-11-06"))
        self.assertFalse(tracker.is_weekly_locked("2026-11-06"))

    def test_limit_detection_works(self):
        tracker = main.GameUsageTracker(db_path=":memory:")
        tracker.set_limits(daily_minutes=60, weekly_minutes=180)

        tracker.log_session("2026-11-06", 30)
        tracker.log_session("2026-11-06", 35)

        self.assertTrue(tracker.is_daily_locked("2026-11-06"))
        self.assertEqual(tracker.get_remaining_daily("2026-11-06"), 0)
        self.assertEqual(tracker.get_remaining_weekly("2026-11-06"), 115)

    def test_duration_formatting(self):
        self.assertEqual(main.format_duration(3661), "01:01:01")
        self.assertEqual(main.format_duration(90), "00:01:30")


if __name__ == "__main__":
    unittest.main()
