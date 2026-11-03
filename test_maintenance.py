from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
from storage import CONTENT_SQL


class ContentPredicateTests(unittest.TestCase):
    """The SQL filter must agree with what the dashboard calls an empty recording."""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE segments (transcript TEXT, conversation_analysis TEXT)")
        rows = [
            ("", None),                                   # silent fragment
            (None, None),                                 # failed, never transcribed
            ("今天开会讨论上线时间", None),                  # has a transcript
            (None, '{"title": "项目例会"}'),                # has an analysis title
            (None, '{"title": "空"}'),                     # placeholder title only
        ]
        self.connection.executemany("INSERT INTO segments VALUES (?, ?)", rows)

    def tearDown(self) -> None:
        self.connection.close()

    def test_only_recordings_with_content_are_counted(self) -> None:
        count = self.connection.execute(
            f"SELECT COUNT(*) FROM segments WHERE {CONTENT_SQL}"
        ).fetchone()[0]
        self.assertEqual(count, 2)


class PurgeTests(unittest.TestCase):
    def test_purge_moves_files_to_the_recycle_bin_and_drops_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "silent.wav"
            audio.write_bytes(b"x" * 120)
            rows = [{"id": 7, "path": str(audio), "recorded_at": "2026-01-01T00:00:00+00:00"}]
            with (
                patch.object(maintenance, "empty_segments", return_value=rows),
                patch.object(maintenance, "recycle_paths", return_value=([audio], 120)) as recycle,
                patch.object(maintenance, "delete_segments") as delete,
            ):
                plan = maintenance.purge_empty_segments(30)

            self.assertEqual(plan.count, 1)
            self.assertEqual(plan.bytes, 120)
            recycle.assert_called_once()
            delete.assert_called_once_with([7])

    def test_files_that_could_not_be_recycled_keep_their_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "busy.wav"
            audio.write_bytes(b"x" * 10)
            rows = [{"id": 3, "path": str(audio), "recorded_at": "2026-01-01T00:00:00+00:00"}]
            with (
                patch.object(maintenance, "empty_segments", return_value=rows),
                patch.object(maintenance, "recycle_paths", return_value=([], 0)),
                patch.object(maintenance, "delete_segments") as delete,
            ):
                plan = maintenance.purge_empty_segments(30)

            self.assertEqual(plan.count, 0)
            delete.assert_not_called()

    def test_nothing_to_do_returns_empty_plan(self) -> None:
        with patch.object(maintenance, "empty_segments", return_value=[]):
            plan = maintenance.purge_empty_segments(30)
        self.assertEqual((plan.count, plan.bytes), (0, 0))


class ScheduledCleanupTests(unittest.TestCase):
    def test_runs_once_per_day_after_the_cleanup_hour(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "maintenance-state.json"
            with (
                patch.object(maintenance, "STATE_PATH", state_path),
                patch.object(maintenance, "purge_empty_segments", return_value=maintenance.PurgePlan(3, 300)) as purge,
            ):
                self.assertIsNone(maintenance.run_scheduled_cleanup(hour=23))
                self.assertIsNotNone(maintenance.run_scheduled_cleanup(hour=0))
                self.assertIsNone(maintenance.run_scheduled_cleanup(hour=0))
                self.assertEqual(purge.call_count, 1)
            self.assertTrue(state_path.exists())


if __name__ == "__main__":
    unittest.main()
