from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from config import DB_PATH, ensure_storage


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    ensure_storage()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                recorded_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                dbfs REAL,
                active_seconds REAL,
                status TEXT NOT NULL DEFAULT 'queued',
                transcript TEXT,
                speech_analysis TEXT,
                summary TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(segments)").fetchall()}
        if "speech_analysis" not in columns:
            conn.execute("ALTER TABLE segments ADD COLUMN speech_analysis TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS period_summaries (
                period_type TEXT NOT NULL,
                period_start TEXT NOT NULL,
                summary TEXT NOT NULL,
                provider TEXT NOT NULL,
                source_segment_count INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                PRIMARY KEY (period_type, period_start)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_summaries (
                day TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                source_segment_count INTEGER NOT NULL,
                generated_at TEXT NOT NULL
            )
            """
        )


def add_segment(path: Path, recorded_at: datetime, duration_seconds: float, status: str = "queued") -> None:
    timestamp = now()
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO segments
               (path, recorded_at, duration_seconds, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(path), recorded_at.astimezone(timezone.utc).isoformat(), duration_seconds, status, timestamp, timestamp),
        )


def update_segment(source_path: Path, **fields: object) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect() as conn:
        conn.execute(f"UPDATE segments SET {assignments} WHERE path = ?", [*fields.values(), str(source_path)])


def queued_segments(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM segments WHERE status = 'queued' ORDER BY recorded_at LIMIT ?", (limit,)).fetchall()


def requeue_dashscope_waiting_segments() -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE segments SET status = 'queued', error = NULL, updated_at = ? WHERE status = 'awaiting_dashscope_config'",
            (now(),),
        )


def remove_incomplete_segment_records() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM segments WHERE path LIKE ?", ("%.part.wav",))


def dashboard_segments(limit: int = 500) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM segments ORDER BY recorded_at DESC LIMIT ?", (limit,)).fetchall()


def daily_transcript(day: date) -> tuple[str, int]:
    rows = dashboard_segments(limit=5000)
    parts: list[str] = []
    for row in reversed(rows):
        if not row["transcript"]:
            continue
        recorded_at = datetime.fromisoformat(row["recorded_at"]).astimezone().date()
        if recorded_at == day:
            parts.append(row["transcript"])
    return "\n\n".join(parts), len(parts)


def get_daily_summary(day: date) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM daily_summaries WHERE day = ?", (day.isoformat(),)).fetchone()


def save_daily_summary(day: date, summary: str, source_segment_count: int) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO daily_summaries (day, summary, source_segment_count, generated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                 summary = excluded.summary,
                 source_segment_count = excluded.source_segment_count,
                 generated_at = excluded.generated_at""",
            (day.isoformat(), summary, source_segment_count, now()),
        )


def period_transcript(start: datetime, end: datetime) -> tuple[str, int]:
    rows = dashboard_segments(limit=10000)
    parts: list[str] = []
    for row in reversed(rows):
        if not row["transcript"]:
            continue
        recorded_at = datetime.fromisoformat(row["recorded_at"]).astimezone()
        if start <= recorded_at < end:
            parts.append(row["transcript"])
    return "\n\n".join(parts), len(parts)


def get_period_summary(period_type: str, period_start: datetime) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM period_summaries WHERE period_type = ? AND period_start = ?",
            (period_type, period_start.isoformat()),
        ).fetchone()


def save_period_summary(
    period_type: str, period_start: datetime, summary: str, provider: str, source_segment_count: int
) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO period_summaries
               (period_type, period_start, summary, provider, source_segment_count, generated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(period_type, period_start) DO UPDATE SET
                 summary = excluded.summary,
                 provider = excluded.provider,
                 source_segment_count = excluded.source_segment_count,
                 generated_at = excluded.generated_at""",
            (period_type, period_start.isoformat(), summary, provider, source_segment_count, now()),
        )
