from __future__ import annotations

import sqlite3
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from config import DB_PATH, ensure_storage


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    ensure_storage()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with transaction() as conn:
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
                transcript_timeline TEXT,
                conversation_analysis TEXT,
                conversation_analysis_error TEXT,
                summary TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(segments)").fetchall()}
        for column, definition in {
            "path": "TEXT NOT NULL DEFAULT ''",
            "recorded_at": "TEXT NOT NULL DEFAULT ''",
            "duration_seconds": "REAL NOT NULL DEFAULT 0",
            "dbfs": "REAL",
            "active_seconds": "REAL",
            "status": "TEXT NOT NULL DEFAULT 'queued'",
            "transcript": "TEXT",
            "transcript_timeline": "TEXT",
            "conversation_analysis": "TEXT",
            "conversation_analysis_error": "TEXT",
            "summary": "TEXT",
            "error": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
            "lease_owner": "TEXT",
            "lease_expires_at": "TEXT",
            "tags": "TEXT NOT NULL DEFAULT '[]'",
            "favorite": "INTEGER NOT NULL DEFAULT 0",
            "notes": "TEXT",
            "excitement_rating": "INTEGER NOT NULL DEFAULT 1",
            "speaker_aliases": "TEXT NOT NULL DEFAULT '{}'",
        }.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE segments ADD COLUMN {column} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_status_recorded ON segments(status, recorded_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_segments_recorded ON segments(recorded_at)")
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY,
                segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
                start_seconds REAL NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS speakers (
                source_speaker TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summary_versions (
                id INTEGER PRIMARY KEY,
                period_type TEXT NOT NULL,
                period_start TEXT NOT NULL,
                summary TEXT NOT NULL,
                provider TEXT NOT NULL,
                source_segment_count INTEGER NOT NULL,
                generated_at TEXT NOT NULL
            )
            """
        )


def add_segment(path: Path, recorded_at: datetime, duration_seconds: float, status: str = "queued") -> None:
    timestamp = now()
    with transaction() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO segments
               (path, recorded_at, duration_seconds, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(path), recorded_at.astimezone(timezone.utc).isoformat(), duration_seconds, status, timestamp, timestamp),
        )


def update_segment(segment_id: int, **fields: object) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with transaction() as conn:
        conn.execute(f"UPDATE segments SET {assignments} WHERE id = ?", [*fields.values(), segment_id])


def claim_segments(owner: str | None = None, limit: int = 20, lease_seconds: int = 1800) -> list[sqlite3.Row]:
    owner = owner or f"{socket.gethostname()}:{__import__('os').getpid()}"
    expires = datetime.now(timezone.utc).timestamp() + lease_seconds
    lease_until = datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds")
    with transaction() as conn:
        rows = conn.execute(
            """SELECT * FROM segments
               WHERE status = 'queued'
                  OR (status = 'transcribing' AND (lease_expires_at IS NULL OR lease_expires_at <= ?))
               ORDER BY recorded_at LIMIT ?""",
            (now(), limit),
        ).fetchall()
        claimed: list[sqlite3.Row] = []
        for row in rows:
            updated = conn.execute(
                """UPDATE segments SET status = 'transcribing', lease_owner = ?, lease_expires_at = ?,
                   attempts = attempts + 1, error = NULL, updated_at = ?
                   WHERE id = ? AND (status = 'queued' OR (status = 'transcribing' AND
                   (lease_expires_at IS NULL OR lease_expires_at <= ?)))""",
                (owner, lease_until, now(), row["id"], now()),
            )
            if updated.rowcount:
                claimed.append(conn.execute("SELECT * FROM segments WHERE id = ?", (row["id"],)).fetchone())
        return claimed


def queued_segments(limit: int = 20) -> list[sqlite3.Row]:
    """Compatibility wrapper for callers that only need to inspect queued rows."""
    with transaction() as conn:
        return conn.execute("SELECT * FROM segments WHERE status = 'queued' ORDER BY recorded_at LIMIT ?", (limit,)).fetchall()


def requeue_dashscope_waiting_segments() -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE segments SET status = 'queued', error = NULL, lease_owner = NULL, lease_expires_at = NULL, updated_at = ? WHERE status = 'awaiting_dashscope_config'",
            (now(),),
        )


def remove_incomplete_segment_records() -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM segments WHERE path LIKE ?", ("%.part.wav",))


def dashboard_segments(limit: int = 500) -> list[sqlite3.Row]:
    with transaction() as conn:
        return conn.execute("SELECT * FROM segments ORDER BY recorded_at DESC LIMIT ?", (limit,)).fetchall()


def dashboard_overview(limit: int = 120) -> list[sqlite3.Row]:
    """Return only list-view fields; transcripts and analysis are loaded on demand."""
    with transaction() as conn:
        return conn.execute(
            """SELECT id, path, recorded_at, duration_seconds, dbfs, active_seconds, status,
                      favorite, tags, notes,
                      CASE WHEN json_valid(conversation_analysis) THEN excitement_rating ELSE 0 END AS display_rating,
                      CASE WHEN json_valid(conversation_analysis)
                           THEN json_extract(conversation_analysis, '$.title')
                           ELSE NULL END AS display_title
               FROM segments ORDER BY recorded_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def dashboard_counts() -> dict[str, int]:
    with transaction() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM segments GROUP BY status").fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    counts["all"] = sum(counts.values())
    return counts


def find_dashboard_segments(
    query: str = "", statuses: list[str] | None = None, recorded_on: date | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    """Search in SQLite so the browser never receives the entire archive to filter."""
    clauses: list[str] = []
    values: list[object] = []
    normalized_query = query.strip()
    if normalized_query:
        needle = f"%{normalized_query}%"
        clauses.append("(transcript LIKE ? OR conversation_analysis LIKE ? OR notes LIKE ? OR tags LIKE ?)")
        values.extend([needle, needle, needle, needle])
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        values.extend(statuses)
    if recorded_on:
        local_timezone = datetime.now().astimezone().tzinfo
        start = datetime.combine(recorded_on, datetime.min.time(), tzinfo=local_timezone).astimezone(timezone.utc)
        end = datetime.combine(recorded_on, datetime.max.time(), tzinfo=local_timezone).astimezone(timezone.utc)
        clauses.append("recorded_at >= ? AND recorded_at <= ?")
        values.extend([start.isoformat(), end.isoformat()])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(limit)
    with transaction() as conn:
        return conn.execute(f"SELECT * FROM segments{where} ORDER BY recorded_at DESC LIMIT ?", values).fetchall()


def find_dashboard_overview(
    query: str = "",
    statuses: list[str] | None = None,
    recorded_on: date | None = None,
    ratings: list[int] | None = None,
    favorites_only: bool = False,
    newest_first: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    """Filter the entire archive in SQLite while only returning list-view fields."""
    clauses: list[str] = []
    values: list[object] = []
    if query.strip():
        needle = f"%{query.strip()}%"
        clauses.append("(transcript LIKE ? OR conversation_analysis LIKE ? OR notes LIKE ? OR tags LIKE ?)")
        values.extend([needle, needle, needle, needle])
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        values.extend(statuses)
    if recorded_on:
        local_timezone = datetime.now().astimezone().tzinfo
        start = datetime.combine(recorded_on, datetime.min.time(), tzinfo=local_timezone).astimezone(timezone.utc)
        end = datetime.combine(recorded_on, datetime.max.time(), tzinfo=local_timezone).astimezone(timezone.utc)
        clauses.append("recorded_at >= ? AND recorded_at <= ?")
        values.extend([start.isoformat(), end.isoformat()])
    if ratings:
        rating_cases = ",".join("?" for _ in ratings)
        clauses.append(f"CASE WHEN json_valid(conversation_analysis) THEN excitement_rating ELSE 0 END IN ({rating_cases})")
        values.extend(ratings)
    if favorites_only:
        clauses.append("favorite = 1")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    direction = "DESC" if newest_first else "ASC"
    columns = """id, path, recorded_at, duration_seconds, dbfs, active_seconds, status,
                 favorite, tags, notes,
                 CASE WHEN json_valid(conversation_analysis) THEN excitement_rating ELSE 0 END AS display_rating,
                 CASE WHEN json_valid(conversation_analysis)
                      THEN json_extract(conversation_analysis, '$.title')
                      ELSE NULL END AS display_title"""
    with transaction() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM segments{where}", values).fetchone()[0])
        rows = conn.execute(
            f"SELECT {columns} FROM segments{where} ORDER BY recorded_at {direction} LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
    return rows, total


def dashboard_segment(segment_id: int) -> sqlite3.Row | None:
    with transaction() as conn:
        return conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone()


def update_segments(segment_ids: list[int], **fields: object) -> int:
    if not segment_ids or not fields:
        return 0
    fields["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    placeholders = ",".join("?" for _ in segment_ids)
    with transaction() as conn:
        result = conn.execute(f"UPDATE segments SET {assignments} WHERE id IN ({placeholders})", [*fields.values(), *segment_ids])
        return result.rowcount


def delete_segments(segment_ids: list[int]) -> list[str]:
    if not segment_ids:
        return []
    placeholders = ",".join("?" for _ in segment_ids)
    with transaction() as conn:
        rows = conn.execute(f"SELECT path FROM segments WHERE id IN ({placeholders})", segment_ids).fetchall()
        conn.execute(f"DELETE FROM segments WHERE id IN ({placeholders})", segment_ids)
        return [str(row["path"]) for row in rows]


def add_bookmark(segment_id: int, start_seconds: float, label: str = "", note: str = "") -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO bookmarks(segment_id, start_seconds, label, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (segment_id, max(0, start_seconds), label.strip(), note.strip(), now()),
        )


def bookmarks_for_segment(segment_id: int) -> list[sqlite3.Row]:
    with transaction() as conn:
        return conn.execute("SELECT * FROM bookmarks WHERE segment_id = ? ORDER BY start_seconds", (segment_id,)).fetchall()


def list_speakers() -> dict[str, str]:
    with transaction() as conn:
        return {str(row["source_speaker"]): str(row["display_name"]) for row in conn.execute("SELECT * FROM speakers")}


def save_speaker_alias(source_speaker: str, display_name: str) -> None:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO speakers(source_speaker, display_name, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(source_speaker) DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at""",
            (source_speaker, display_name.strip(), now()),
        )


def save_summary_version(period_type: str, period_start: datetime, summary: str, provider: str, source_count: int) -> None:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO summary_versions(period_type, period_start, summary, provider, source_segment_count, generated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (period_type, period_start.isoformat(), summary, provider, source_count, now()),
        )


def summary_versions(period_type: str, period_start: datetime) -> list[sqlite3.Row]:
    with transaction() as conn:
        return conn.execute(
            "SELECT * FROM summary_versions WHERE period_type = ? AND period_start = ? ORDER BY generated_at DESC",
            (period_type, period_start.isoformat()),
        ).fetchall()


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
    with transaction() as conn:
        return conn.execute("SELECT * FROM daily_summaries WHERE day = ?", (day.isoformat(),)).fetchone()


def save_daily_summary(day: date, summary: str, source_segment_count: int) -> None:
    with transaction() as conn:
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
    with transaction() as conn:
        return conn.execute(
            "SELECT * FROM period_summaries WHERE period_type = ? AND period_start = ?",
            (period_type, period_start.isoformat()),
        ).fetchone()


def save_period_summary(
    period_type: str, period_start: datetime, summary: str, provider: str, source_segment_count: int
) -> None:
    with transaction() as conn:
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
