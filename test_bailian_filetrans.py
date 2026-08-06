from __future__ import annotations

import unittest

from bailian_filetrans import _timeline_from_sources


class TimelineExtractionTests(unittest.TestCase):
    def test_extracts_and_sorts_qwen_sentences(self) -> None:
        source = {
            "transcripts": [
                {
                    "sentences": [
                        {"begin_time": 3200, "end_time": 4800, "text": "第二句话。", "speaker_id": 1},
                        {"begin_time": 400, "end_time": 2100, "text": "第一句话。", "speaker_id": 0},
                    ]
                }
            ]
        }

        self.assertEqual(
            _timeline_from_sources([source]),
            [
                {"text": "第一句话。", "start_seconds": 0.4, "end_seconds": 2.1, "speaker": "0"},
                {"text": "第二句话。", "start_seconds": 3.2, "end_seconds": 4.8, "speaker": "1"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
