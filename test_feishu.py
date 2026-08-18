from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from feishu import CARD_CHUNK_SIZE, _analysis_projection, _archive_fields, report_card


class FeishuCardTests(unittest.TestCase):
    def test_report_card_splits_long_content_without_losing_text(self) -> None:
        body = "A" * (CARD_CHUNK_SIZE + 10)
        card = report_card("Test", body)
        contents = [element["text"]["content"] for element in card["elements"] if element["tag"] == "div"]
        self.assertEqual("".join(contents), body)
        self.assertTrue(all(len(content) <= CARD_CHUNK_SIZE for content in contents))

    def test_analysis_projection_keeps_people_topics_events_and_actions(self) -> None:
        projection = _analysis_projection(
            {
                "overview": "讨论发布安排",
                "key_people": [{"name_or_label": "小王", "background": "产品负责人"}],
                "topics": [{"title": "发布", "summary": "确定周五上线", "action_items": [{"task": "准备公告", "owner": "小王", "due_date": "周四"}]}],
                "noteworthy_events": [{"title": "上线时间确定", "details": "周五发布"}],
                "highlight_quotes": ["周五必须上线。"],
            }
        )
        self.assertIn("小王", projection["people"])
        self.assertIn("周五上线", projection["topics"])
        self.assertIn("准备公告", projection["actions"])
        self.assertIn("上线时间确定", projection["events"])
        self.assertIn("周五必须上线", projection["detailed"])

    def test_archive_fields_include_a_human_readable_timestamp(self) -> None:
        record = {
            "id": 1,
            "recorded_at": datetime(2026, 8, 14, 9, 5, 3, tzinfo=timezone.utc).isoformat(),
            "duration_seconds": 60,
            "active_seconds": 10,
            "status": "complete",
            "excitement_rating": 3,
            "conversation_analysis": "",
            "summary": "",
            "tags": "[]",
            "favorite": 0,
            "notes": "",
            "transcript": "",
            "path": Path("recording.wav"),
        }
        fields = _archive_fields(record, "标题")
        self.assertEqual(
            fields["录音时间（精确）"],
            datetime(2026, 8, 14, 9, 5, 3, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        )


if __name__ == "__main__":
    unittest.main()
