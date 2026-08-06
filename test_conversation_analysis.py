from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import gc
from pathlib import Path

import storage
from worker import _json_object, conversation_source


class ConversationAnalysisTests(unittest.TestCase):
    def test_conversation_source_keeps_time_and_original_speaker(self) -> None:
        timeline = json.dumps([
            {"start_seconds": 65.4, "speaker": "2", "text": "我们明天再确认。"},
        ])

        self.assertEqual(
            conversation_source("备用逐字稿", timeline),
            "[01:05] 原始说话人 2：我们明天再确认。",
        )

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
