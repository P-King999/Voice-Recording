from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import report_book


def row(moment: datetime, duration: float, active: float, status: str = "complete") -> dict[str, object]:
    return {
        "recorded_at": moment.astimezone(timezone.utc).isoformat(),
        "duration_seconds": duration,
        "active_seconds": active,
        "status": status,
    }


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute).astimezone()


class DayStatsTests(unittest.TestCase):
    def test_days_are_split_into_morning_afternoon_evening(self) -> None:
        rows = [
            row(local(2026, 11, 3, 9), 600, 120),
            row(local(2026, 11, 3, 14), 60, 0, "silent"),
            row(local(2026, 11, 3, 20), 300, 30),
        ]
        with patch.object(report_book, "_range_rows", return_value=rows):
            stats = report_book.day_stats(date(2026, 11, 3), local(2026, 11, 3, 21))

        self.assertEqual(stats.segments, 3)
        self.assertEqual(stats.recorded, 960)
        self.assertEqual(stats.active, 150)
        self.assertEqual(stats.transcribed, 2)
        self.assertEqual([span[2] for span in stats.spans], [1, 1, 1])
        self.assertFalse(stats.silent_only)

    def test_a_day_without_segments_is_marked_as_unrecorded(self) -> None:
        with patch.object(report_book, "_range_rows", return_value=[]):
            stats = report_book.day_stats(date(2026, 11, 2), local(2026, 11, 3, 9))

        self.assertEqual(stats.segments, 0)
        self.assertTrue(all(span[2] == 0 for span in stats.spans))


class DailyPageTests(unittest.TestCase):
    def test_missing_day_keeps_a_page_instead_of_disappearing(self) -> None:
        stats = report_book.DayStats(day=date(2026, 11, 2))
        body = report_book.daily_markdown(stats, None, local(2026, 11, 3, 9))

        self.assertIn("# 2026-11-02 周一", body)
        self.assertIn("当天没有录音分段", body)
        self.assertIn("书里不会缺页", body)

    def test_silent_day_explains_the_microphone(self) -> None:
        stats = report_book.DayStats(day=date(2026, 11, 1), segments=20, recorded=3600, active=0.0)
        body = report_book.daily_markdown(stats, None, local(2026, 11, 3, 9))

        self.assertIn("没有检测到声音", body)
        self.assertIn("麦克风", body)

    def test_placeholder_summary_is_not_shown_as_content(self) -> None:
        stats = report_book.DayStats(day=date(2026, 11, 2))
        body = report_book.daily_markdown(
            stats, {"summary": "空", "provider": "system", "source_segment_count": 0}, local(2026, 11, 3, 9)
        )
        self.assertNotIn("\n空\n", body)
        self.assertIn("没有可用的逐字稿", body)

    def test_today_says_it_is_still_running(self) -> None:
        stats = report_book.DayStats(day=date(2026, 11, 3), segments=1, recorded=60, active=5)
        body = report_book.daily_markdown(stats, None, local(2026, 11, 3, 15, 30))
        self.assertIn("仍在继续", body)
        self.assertIn("15:30", body)


class WeeklyPageTests(unittest.TestCase):
    def test_future_days_are_not_reported_as_missing(self) -> None:
        monday = date(2026, 11, 2)
        days = [report_book.DayStats(day=monday + timedelta(days=offset)) for offset in range(7)]
        days[1].segments = 4
        days[1].recorded = 600
        days[1].active = 60
        days[1].transcribed = 2

        body = report_book.weekly_markdown(monday, days, None, local(2026, 11, 3, 15))

        self.assertIn("第 45 周", body)
        self.assertIn("尚未到这一天", body)
        self.assertIn("11-02", body)          # yesterday has no audio and is listed
        self.assertNotIn("11-08", body.split("## 四、提示")[-1])

    def test_completed_week_lists_every_day(self) -> None:
        monday = date(2026, 10, 26)
        days = [report_book.DayStats(day=monday + timedelta(days=offset)) for offset in range(7)]
        for day in days:
            day.segments = 8
            day.recorded = 3600
            day.active = 0.0
        body = report_book.weekly_markdown(monday, days, None, local(2026, 11, 3, 15))

        self.assertIn("全部为静音", body)
        for offset in range(7):
            self.assertIn(f"{monday + timedelta(days=offset):%m-%d}", body)


class ExportTests(unittest.TestCase):
    def test_book_files_and_index_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            written = report_book.write_book(Path(temporary), days=2, weeks=1)

            names = sorted(path.name for path in written)
            self.assertIn("目录.md", names)
            self.assertEqual(len([name for name in names if name.endswith(".md")]), 4)
            index = (Path(temporary) / "目录.md").read_text(encoding="utf-8")
            self.assertIn("## 日报", index)
            self.assertIn("## 周报", index)


if __name__ == "__main__":
    unittest.main()
