import unittest
from datetime import date
from unittest.mock import patch

import main


class GameUsageTrackerTests(unittest.TestCase):
    def test_web_app_opens_in_browser_on_startup(self):
        server = unittest.mock.Mock()
        server.server_port = 8000
        server.serve_forever.side_effect = KeyboardInterrupt

        with (
            patch("sys.argv", ["main.py"]),
            patch.object(main, "WebGameTimeManager"),
            patch.object(main, "GameTimeHTTPServer", return_value=server),
            patch.object(main.webbrowser, "open", return_value=True) as open_browser,
        ):
            main.main()

        open_browser.assert_called_once_with("http://127.0.0.1:8000/")
        server.server_close.assert_called_once()

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

    def test_current_week_usage_is_returned_monday_through_sunday(self):
        tracker = main.GameUsageTracker(db_path=":memory:")
        tracker.log_session("2026-11-09", 30)
        tracker.log_session("2026-11-15", 45)
        tracker.log_session("2026-11-16", 60)

        history = tracker.get_current_week_usage_by_day("2026-11-13")

        self.assertEqual(
            [(item["weekday"], item["date"], item["usage"]) for item in history],
            [
                ("月", "2026-11-09", 30),
                ("火", "2026-11-10", 0),
                ("水", "2026-11-11", 0),
                ("木", "2026-11-12", 0),
                ("金", "2026-11-13", 0),
                ("土", "2026-11-14", 0),
                ("日", "2026-11-15", 45),
            ],
        )
        tracker.conn.close()

    def test_notification_settings_are_persisted_and_validated(self):
        tracker = main.GameUsageTracker(db_path=":memory:")
        self.assertEqual(
            tracker.get_notification_settings(),
            {"tone": "chime", "volume": 50},
        )

        tracker.set_notification_settings("soft", 35)

        self.assertEqual(
            tracker.get_notification_settings(),
            {"tone": "soft", "volume": 35},
        )
        with self.assertRaises(ValueError):
            tracker.set_notification_settings("unknown", 35)
        with self.assertRaises(ValueError):
            tracker.set_notification_settings("bell", 101)
        tracker.conn.close()

    def test_web_status_includes_notification_settings(self):
        tracker = main.GameUsageTracker(db_path=":memory:")
        tracker.set_notification_settings("bell", 72)
        manager = main.WebGameTimeManager(tracker, process_provider=lambda: set())

        self.assertEqual(
            manager.get_status()["notification_settings"],
            {"tone": "bell", "volume": 72},
        )
        tracker.conn.close()

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

    def test_web_manager_tracks_configured_process_automatically(self):
        tracker = main.GameUsageTracker(db_path=":memory:")
        tracker.set_weekday_limits([60, 120, 180, 240, 300])
        tracker.set_process_names(["game.exe"])
        running = {"game.exe"}
        manager = main.WebGameTimeManager(tracker, process_provider=lambda: running)
        now = time.time()
        manager.scan_processes(now=now, force=True)
        manager.scan_processes(now=now + 60, force=True)
        running.clear()
        manager.scan_processes(now=now + 120, force=True)

        self.assertEqual(tracker.get_week_usage_seconds(), 120)
        self.assertFalse(manager.get_status()["active"])
        self.assertEqual(tracker.get_weekday_limits(), [60, 120, 180, 240, 300])
        tracker.conn.close()

    def test_web_manager_rejects_invalid_settings(self):
        tracker = main.GameUsageTracker(db_path=":memory:")
        manager = main.WebGameTimeManager(tracker)
        with self.assertRaises(ValueError):
            manager.save_settings([0, 0, float("inf"), 0, 0], ["game.exe"])
        with self.assertRaises(ValueError):
            manager.save_settings([1, 1, 1, 1, 1], ["../game.exe"])
        tracker.conn.close()


if __name__ == "__main__":
    unittest.main()
