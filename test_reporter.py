from __future__ import annotations

import unittest
from datetime import datetime, time, timezone

from reporter import due_schedule, report_summary_window


class ReporterScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.day = datetime(2026, 8, 17, 12, 10, 20, tzinfo=timezone.utc)

    def test_noon_schedule_remains_the_morning_report_after_polling_delay(self) -> None:
        period, start, end = report_summary_window(self.day, time(12, 10))
        self.assertEqual(period, "morning")
        self.assertEqual((start.hour, end.hour), (0, 12))

    def test_afternoon_and_final_schedules_use_closed_periods(self) -> None:
        afternoon, _, afternoon_end = report_summary_window(self.day, time(18, 10))
        final, _, final_end = report_summary_window(self.day, time(21, 10))
        self.assertEqual((afternoon, afternoon_end.hour), ("afternoon", 17))
        self.assertEqual((final, final_end.hour), ("day", 21))

    def test_due_schedule_returns_the_trigger_time_with_the_key(self) -> None:
        due = due_schedule(self.day, {})
        self.assertIsNotNone(due)
        assert due is not None
        self.assertEqual(due[1], time(12, 10))


if __name__ == "__main__":
    unittest.main()
