from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import gc
from datetime import datetime, timezone
from pathlib import Path

import storage
from worker import _excitement_rating, _json_object, analyze_transcript_conversation, conversation_source


class ConversationAnalysisTests(unittest.TestCase):
    def test_excitement_rating_is_normalized(self) -> None:
        self.assertEqual(_excitement_rating('{"excitement_rating": 4}'), 4)
        self.assertEqual(_excitement_rating('{"excitement_rating": 99}'), 5)
        self.assertEqual(_excitement_rating('{"excitement_rating": "unknown"}'), 1)

    def test_claim_and_reclaim_expired_segment(self) -> None:
        original_db_path = storage.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            storage.DB_PATH = Path(directory) / "voice_recorder.sqlite3"
            try:
                storage.init_db()
                storage.add_segment(Path(directory) / "sample.wav", datetime.now(timezone.utc), 3)
                first = storage.claim_segments(owner="test-a", lease_seconds=60)
                self.assertEqual(len(first), 1)
                storage.update_segment(first[0]["id"], lease_expires_at="2000-01-01T00:00:00+00:00")
                second = storage.claim_segments(owner="test-b", lease_seconds=60)
                self.assertEqual([row["id"] for row in second], [first[0]["id"]])
                self.assertEqual(second[0]["lease_owner"], "test-b")
            finally:
                storage.DB_PATH = original_db_path
                gc.collect()

    def test_startup_requeues_interrupted_transcription(self) -> None:
        original_db_path = storage.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            storage.DB_PATH = Path(directory) / "voice_recorder.sqlite3"
            try:
                storage.init_db()
                storage.add_segment(Path(directory) / "sample.wav", datetime.now(timezone.utc), 3)
                claimed = storage.claim_segments(owner="old-worker", lease_seconds=3600)
                storage.requeue_interrupted_segments()
                resumed = storage.claim_segments(owner="new-worker", lease_seconds=60)
                self.assertEqual([row["id"] for row in resumed], [claimed[0]["id"]])
                self.assertEqual(resumed[0]["lease_owner"], "new-worker")
            finally:
                storage.DB_PATH = original_db_path
                gc.collect()

    def test_conversation_source_keeps_time_and_original_speaker(self) -> None:
        timeline = json.dumps([
            {"start_seconds": 65.4, "speaker": "2", "text": "我们明天再确认。"},
        ])

        self.assertEqual(
            conversation_source("备用逐字稿", timeline),
            "[01:05] 原始说话人 2：我们明天再确认。",
        )

    def test_empty_transcript_is_normalized_and_rejected_before_request(self) -> None:
        self.assertEqual(conversation_source(None, None), "")
        with self.assertRaisesRegex(ValueError, "没有可用的逐字稿"):
            analyze_transcript_conversation(None, None)

    def test_json_object_accepts_a_fenced_response(self) -> None:
        self.assertEqual(_json_object("```json\n{\"overview\": \"测试\"}\n```"), {"overview": "测试"})

    def test_init_db_migrates_conversation_columns(self) -> None:
        original_db_path = storage.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "voice_recorder.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE segments (id INTEGER PRIMARY KEY)")
            finally:
                connection.close()
            storage.DB_PATH = database
            try:
                storage.init_db()
                connection = sqlite3.connect(database)
                try:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(segments)")}
                finally:
                    connection.close()
                self.assertTrue({"conversation_analysis", "conversation_analysis_error"}.issubset(columns))
            finally:
                storage.DB_PATH = original_db_path
                gc.collect()


if __name__ == "__main__":
    unittest.main()
