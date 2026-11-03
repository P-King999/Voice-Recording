from __future__ import annotations

import unittest
from datetime import datetime, time, timezone
from unittest.mock import patch

import reporter


def local(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """Wall-clock time in the machine's zone, matching what the reporter stores."""
    return datetime(year, month, day, hour, minute).astimezone()


class ReportWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.slots = [time(12, 10), time(18, 10)]

    def test_slot_reports_everything_since_the_previous_slot(self) -> None:
        window = reporter.window_for_slot(local(2026, 11, 3, 18, 10), self.slots)
        self.assertEqual(window.start, local(2026, 11, 3, 12, 10))
        self.assertEqual(window.end, local(2026, 11, 3, 18, 10))
        self.assertEqual(window.label, "11-03 12:10 → 18:10")

    def test_first_slot_of_the_day_covers_the_previous_evening(self) -> None:
        window = reporter.window_for_slot(local(2026, 11, 3, 12, 10), self.slots)
        self.assertEqual(window.start, local(2026, 11, 2, 18, 10))
        self.assertEqual(window.end, local(2026, 11, 3, 12, 10))

    def test_nothing_is_due_right_after_a_delivered_report(self) -> None:
        state = {"last_report": "2026-11-03-12:10"}
        self.assertIsNone(reporter.pending_report(local(2026, 11, 3, 12, 10), state, self.slots))

    def test_a_report_is_due_after_the_next_trigger(self) -> None:
        window = reporter.pending_report(
            local(2026, 11, 3, 18, 10), {"last_report": "2026-11-03-12:10"}, self.slots
        )
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.keys, ["2026-11-03-18:10"])
        self.assertEqual((window.start, window.end), (local(2026, 11, 3, 12, 10), local(2026, 11, 3, 18, 10)))
        self.assertFalse(window.is_catchup)

    def test_missed_triggers_merge_into_one_catch_up_range(self) -> None:
        # The machine was off from 11-01 evening until 11-03 15:17.
        window = reporter.pending_report(
            local(2026, 11, 3, 15, 17), {"last_report": "2026-11-01-18:10"}, self.slots
        )
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(window.start, local(2026, 11, 1, 18, 10))
        self.assertEqual(window.end, local(2026, 11, 3, 12, 10))
        self.assertEqual(
            window.keys, ["2026-11-02-12:10", "2026-11-02-18:10", "2026-11-03-12:10"]
        )
        self.assertTrue(window.is_catchup)

    def test_catch_up_starts_at_the_last_delivered_slot(self) -> None:
        # Yesterday's 18:10 report was sent; the machine was off when 12:10 fired today.
        window = reporter.pending_report(
            local(2026, 11, 3, 15, 17), {"last_report": "2026-11-02-18:10"}, self.slots
        )
        assert window is not None
        self.assertEqual(window.start, local(2026, 11, 2, 18, 10))
        self.assertEqual(window.end, local(2026, 11, 3, 12, 10))

    def test_late_delivery_is_reported_as_catch_up(self) -> None:
        window = reporter.pending_report(
            local(2026, 11, 3, 15, 17), {"last_report": "2026-11-02-18:10"}, self.slots
        )
        assert window is not None
        # The 12:10 trigger fired at 15:17, far later than planned.
        self.assertGreater(window.late_by.total_seconds(), 60 * 15)
        self.assertTrue(window.is_catchup)


class ReportBodyTests(unittest.TestCase):
    def test_body_describes_exactly_one_range(self) -> None:
        window = reporter.ReportWindow(local(2026, 11, 3, 12, 10), local(2026, 11, 3, 18, 10), ["2026-11-03-18:10"])
        snapshot = {
            "database_ok": True,
            "database_error": "",
            "free_gb": 100.0,
            "services": {"recorder": True, "reporter": True},
        }
        with (
            patch.object(reporter, "_local_rows", return_value=[]),
            patch.object(reporter, "snapshot", return_value=snapshot),
            patch.object(reporter, "_window_summary", return_value=(None, None)),
            patch.object(reporter, "_recorder_log_lines", return_value=[]),
        ):
            subject, body = reporter.build_report(now=local(2026, 11, 3, 18, 10), window=window)

        self.assertIn("11-03 12:10 → 18:10", body)
        self.assertIn("没有新的录音分段", body)
        self.assertIn("11-03 12:10", subject)


if __name__ == "__main__":
    unittest.main()
