from __future__ import annotations

import html
from datetime import datetime


def _timeline(record: object) -> list[dict[str, object]]:
    import json
    try:
        value = json.loads(record["transcript_timeline"] or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict) and isinstance(item.get("text"), str)]


def markdown(records: list[object]) -> str:
    parts: list[str] = ["# 录音档案", ""]
    for record in sorted(records, key=lambda item: item["recorded_at"]):
        timestamp = datetime.fromisoformat(record["recorded_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        parts.extend([f"## {timestamp}", "", record["transcript"] or "（无逐字稿）", ""])
    return "\n".join(parts)


def srt(records: list[object]) -> str:
    lines: list[str] = []
    number = 1
    for record in sorted(records, key=lambda item: item["recorded_at"]):
        for item in _timeline(record):
            start = float(item.get("start_seconds", 0))
            end = float(item.get("end_seconds", start + 3))
            def stamp(value: float) -> str:
                hours, remainder = divmod(max(0, value), 3600)
                minutes, seconds = divmod(remainder, 60)
                return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{int((value % 1) * 1000):03d}"
            lines.extend([str(number), f"{stamp(start)} --> {stamp(max(end, start + 0.1))}", str(item["text"]), ""])
            number += 1
    return "\n".join(lines)


def word_html(records: list[object]) -> str:
    body = ["<html><meta charset='utf-8'><body><h1>录音档案</h1>"]
    for record in sorted(records, key=lambda item: item["recorded_at"]):
        timestamp = datetime.fromisoformat(record["recorded_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        body.append(f"<h2>{html.escape(timestamp)}</h2><p>{html.escape(record['transcript'] or '（无逐字稿）')}</p>")
    body.append("</body></html>")
    return "".join(body)
