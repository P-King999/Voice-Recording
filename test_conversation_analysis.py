from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import gc
from datetime import datetime, timezone
from pathlib import Path

import storage
import rating_templates
from worker import _excitement_rating, _json_object, analyze_transcript_conversation, conversation_source
from rating_templates import rating_instruction


class ConversationAnalysisTests(unittest.TestCase):
    def test_excitement_rating_is_normalized(self) -> None:
        self.assertEqual(_excitement_rating('{"excitement_rating": 4}'), 4)
        self.assertEqual(_excitement_rating('{"excitement_rating": 0}'), 0)
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

    def test_tag_filter_uses_user_created_tags(self) -> None:
        original_db_path = storage.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            storage.DB_PATH = Path(directory) / "voice_recorder.sqlite3"
            try:
                storage.init_db()
                first_path = Path(directory) / "first.wav"
                second_path = Path(directory) / "second.wav"
                storage.add_segment(first_path, datetime.now(timezone.utc), 3)
                storage.add_segment(second_path, datetime.now(timezone.utc), 3)
                first, second = storage.dashboard_segments()
                storage.update_segment(first["id"], tags=json.dumps(["客户", "重要"]))
                storage.update_segment(second["id"], tags=json.dumps(["内部"]))
                rows, total = storage.find_dashboard_overview(tags=["客户"])
                self.assertEqual(total, 1)
                self.assertEqual(rows[0]["id"], first["id"])
                self.assertEqual(storage.list_segment_tags(), ["内部", "客户", "重要"])
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

    def test_custom_rating_template_overrides_the_preset(self) -> None:
        self.assertEqual(rating_instruction("custom", "0=普通，5=紧急"), "0=普通，5=紧急")
        self.assertIn("重要", rating_instruction("general"))
        self.assertEqual(rating_instruction("gossip"), rating_instruction("author_gossip"))

    def test_user_rating_templates_can_be_saved_and_edited(self) -> None:
        original_path = rating_templates.TEMPLATES_PATH
        with tempfile.TemporaryDirectory() as directory:
            rating_templates.TEMPLATES_PATH = Path(directory) / "rating_templates.json"
            try:
                template_id = rating_templates.save_user_template(None, "我的模板", "0=无内容，5=最重要")
                self.assertTrue(template_id.startswith("user_"))
                self.assertEqual(rating_templates.template_label(template_id), "我的模板")
                self.assertIn("最重要", rating_templates.rating_instruction(template_id))
                rating_templates.save_user_template(template_id, "修改后", "0=普通，5=紧急")
                self.assertEqual(rating_templates.template_label(template_id), "修改后")
            finally:
                rating_templates.TEMPLATES_PATH = original_path

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
