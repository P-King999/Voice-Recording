from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import config
import storage
import summary_scheduler


class SummarySchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "summaries.sqlite3"
        self.db_path_patch = patch.object(storage, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        storage.init_db()

    def tearDown(self) -> None:
        self.db_path_patch.stop()
        self.temporary.cleanup()

    def test_sunday_includes_all_windows_and_week(self) -> None:
        now = datetime(2026, 8, 16, 21, 10, tzinfo=timezone.utc)
        self.assertEqual(
            [window.period_type for window in summary_scheduler.scheduled_windows(now)],
            ["morning", "afternoon", "evening", "day", "week"],
        )

    def test_empty_period_is_saved_once(self) -> None:
        now = datetime(2026, 8, 17, 12, 2, tzinfo=timezone.utc)
        window = summary_scheduler.scheduled_windows(now)[0]
        self.assertTrue(summary_scheduler.generate_window(window, now))
        self.assertFalse(summary_scheduler.generate_window(window, now))
        summary = storage.get_period_summary("morning", window.start)
        self.assertEqual(summary["summary"], "空")
        self.assertEqual(summary["source_segment_count"], 0)


if __name__ == "__main__":
    unittest.main()
