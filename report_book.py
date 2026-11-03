from __future__ import annotations

"""Daily and weekly report pages for the dashboard and for plain-text export.

Every calendar day gets a page, including days when the machine was off, so the
book never has a missing page: an empty day is rendered as an explicit note
instead of silently disappearing.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from config import ROOT
from storage import connect, get_period_summary

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
DAY_SPANS = (
    ("上午", "00:00–12:00", 0, 12),
    ("下午", "12:00–17:00", 12, 17),
    ("晚上", "17:00–24:00", 17, 24),
)
STATUS_LABELS = {
    "queued": "等待处理",
    "transcribing": "转写中",
    "complete": "已完成",
    "silent": "已过滤静音",
    "failed": "处理失败",
    "awaiting_dashscope_config": "等待转写配置",
}


def _midnight(day: date, tz: timezone | None = None) -> datetime:
    moment = datetime.combine(day, time.min)
    return moment.replace(tzinfo=tz) if tz else moment.astimezone()


def _range_rows(start: datetime, end: datetime) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """SELECT recorded_at, duration_seconds, active_seconds, status FROM segments
               WHERE recorded_at >= ? AND recorded_at < ?
               ORDER BY recorded_at""",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        ).fetchall()


@dataclass
class DayStats:
    day: date
    segments: int = 0
    recorded: float = 0.0
    active: float = 0.0
    transcribed: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    spans: list[tuple[str, str, int, float, float]] = field(default_factory=list)
    first: datetime | None = None
    last: datetime | None = None

    @property
    def weekday(self) -> str:
        return WEEKDAY_NAMES[self.day.weekday()]

    @property
    def silent_only(self) -> bool:
        return self.segments > 0 and self.active == 0

    @property
    def idle_hours(self) -> float:
        """Hours of the day that have no recording at all."""
        if self.segments == 0:
            return 24.0
        return max(0.0, (self.last - self.first).total_seconds() / 3600 if self.first and self.last else 0.0)


def day_stats(day: date, now: datetime | None = None) -> DayStats:
    now = now or datetime.now().astimezone()
    start = _midnight(day, now.tzinfo)
    end = min(start + timedelta(days=1), now) if day == now.date() else start + timedelta(days=1)
    rows = _range_rows(start, end) if end > start else []
    stats = DayStats(day=day)
    buckets = {
        index: {"segments": 0, "recorded": 0.0, "active": 0.0}
        for index in range(len(DAY_SPANS))
    }
    for row in rows:
        recorded_at = datetime.fromisoformat(str(row["recorded_at"])).astimezone()
        duration = float(row["duration_seconds"] or 0)
        active = float(row["active_seconds"] or 0)
        status = str(row["status"])
        stats.segments += 1
        stats.recorded += duration
        stats.active += active
        stats.statuses[status] = stats.statuses.get(status, 0) + 1
        if status == "complete":
            stats.transcribed += 1
        stats.first = stats.first or recorded_at
        stats.last = recorded_at
        hour = recorded_at.hour
        for index, (_, _, from_hour, to_hour) in enumerate(DAY_SPANS):
            if from_hour <= hour < to_hour:
                buckets[index]["segments"] += 1
                buckets[index]["recorded"] += duration
                buckets[index]["active"] += active
                break
    stats.spans = [
        (label, span, buckets[index]["segments"], buckets[index]["recorded"], buckets[index]["active"])
        for index, (label, span, _, _) in enumerate(DAY_SPANS)
    ]
    return stats


def hours_text(seconds: float) -> str:
    """Short human readable duration, shared by the dashboard cards."""
    return _hours(seconds)


@dataclass
class RangeStats:
    segments: int = 0
    recorded: float = 0.0
    active: float = 0.0
    transcribed: int = 0
    by_day: list[tuple[date, int, float, float, int]] = field(default_factory=list)


def range_stats(start: datetime, end: datetime) -> RangeStats:
    """Aggregate one arbitrary period, with a per-day breakdown for the tables."""
    stats = RangeStats()
    if end <= start:
        return stats
    buckets: dict[date, list[float]] = {}
    for row in _range_rows(start, end):
        moment = datetime.fromisoformat(str(row["recorded_at"])).astimezone()
        duration = float(row["duration_seconds"] or 0)
        active = float(row["active_seconds"] or 0)
        complete = 1 if str(row["status"]) == "complete" else 0
        stats.segments += 1
        stats.recorded += duration
        stats.active += active
        stats.transcribed += complete
        bucket = buckets.setdefault(moment.date(), [0, 0.0, 0.0, 0])
        bucket[0] += 1
        bucket[1] += duration
        bucket[2] += active
        bucket[3] += complete
    stats.by_day = [(day, int(values[0]), values[1], values[2], int(values[3])) for day, values in sorted(buckets.items())]
    return stats


def week_dates(monday: date) -> list[date]:
    return [monday + timedelta(days=offset) for offset in range(7)]


def current_monday(day: date | None = None) -> date:
    day = day or datetime.now().astimezone().date()
    return day - timedelta(days=day.weekday())


def _hours(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 1:
        return f"{max(0.0, seconds):.0f} 秒"
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def _status_text(statuses: dict[str, int]) -> str:
    if not statuses:
        return "无"
    return "、".join(
        f"{STATUS_LABELS.get(name, name)} {count} 个" for name, count in sorted(statuses.items())
    )


def _summary_block(row: sqlite3.Row | None, label: str, has_segments: bool) -> list[str]:
    text = str(row["summary"]).strip() if row is not None and row["summary"] else ""
    if text in {"", "空", "无", "暂无"}:
        if row is not None:
            return [f"> {label}没有可用的逐字稿（录音可能全部是静音），因此没有摘要。"]
        return [f"> {label}的摘要尚未生成。可以在看板「报告」页点击“重新生成”按钮。"]
    return [
        text,
        "",
        f"*摘要模型：{row['provider']} · 基于 {row['source_segment_count']} 段逐字稿*",
    ]


def day_overview(start: date, end: date) -> dict[date, tuple[int, float]]:
    """Cheap index for the table of contents: segments and recorded seconds per day."""
    rows = _range_rows(_midnight(start), _midnight(end) + timedelta(days=1))
    result: dict[date, tuple[int, float]] = {}
    for row in rows:
        day = datetime.fromisoformat(str(row["recorded_at"])).astimezone().date()
        count, seconds = result.get(day, (0, 0.0))
        result[day] = (count + 1, seconds + float(row["duration_seconds"] or 0))
    return result


def daily_markdown(stats: DayStats, summary_row: sqlite3.Row | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    title = f"{stats.day:%Y-%m-%d} {stats.weekday}"
    coverage = (
        f"覆盖 00:00 → {now:%H:%M}（今天，仍在继续）"
        if stats.day == now.date()
        else "覆盖 00:00 → 24:00（全天）"
    )
    lines = [
        f"# {title}",
        "",
        f"> {coverage} · 录音分段 {stats.segments} 个 · 录音 {_hours(stats.recorded)}",
        "",
        "## 一、当天概况",
        "",
    ]
    if stats.segments == 0:
        lines += [
            "| 项目 | 数值 |",
            "| --- | --- |",
            "| 录音分段 | 0 个 |",
            "",
            "**当天没有录音分段。** 本机可能没有开机，或者录音服务 / 代理没有运行。"
            "这一页仍然保留，书里不会缺页。",
            "",
        ]
    else:
        lines += [
            "| 项目 | 数值 |",
            "| --- | --- |",
            f"| 录音分段 | {stats.segments} 个 |",
            f"| 录音时长 | {_hours(stats.recorded)} |",
            f"| 检测到说话 | {_hours(stats.active)} |",
            f"| 可分析逐字稿 | {stats.transcribed} 段 |",
            f"| 处理状态 | {_status_text(stats.statuses)} |",
            f"| 首段 / 末段 | {stats.first:%H:%M} / {stats.last:%H:%M} |" if stats.first and stats.last else "| 首段 / 末段 | — |",
            "",
        ]
        if stats.silent_only:
            lines += [
                "**当天录音全部没有检测到声音。** 请检查麦克风是否被虚拟声卡占用、"
                "设备是否被静音，或当天确实没有讲话。",
                "",
            ]

    lines += ["## 二、时段分布", "", "| 时段 | 分段 | 录音 | 说话 |", "| --- | --- | --- | --- |"]
    for label, span, count, recorded, active in stats.spans:
        lines.append(f"| {label} {span} | {count} 个 | {_hours(recorded)} | {_hours(active)} |")
    lines += ["", "## 三、当天摘要", "", *_summary_block(summary_row, "当天", stats.segments > 0)]
    return "\n".join(lines)


def weekly_markdown(
    monday: date,
    days: list[DayStats],
    summary_row: sqlite3.Row | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now().astimezone()
    sunday = monday + timedelta(days=6)
    week_number = monday.isocalendar().week
    segments = sum(day.segments for day in days)
    recorded = sum(day.recorded for day in days)
    active = sum(day.active for day in days)
    transcribed = sum(day.transcribed for day in days)
    empty_days = [day for day in days if day.segments == 0 and day.day <= now.date()]
    silent_days = [day for day in days if day.silent_only]
    started_days = [day for day in days if day.day <= now.date()]
    coverage = (
        f"覆盖周一 00:00 → {now:%m-%d %H:%M}（本周进行中）"
        if monday <= now.date() <= sunday
        else "覆盖周一 00:00 → 周日 24:00"
    )

    lines = [
        f"# {monday:%Y-%m-%d} – {sunday:%m-%d} · 第 {week_number} 周",
        "",
        f"> {coverage} · 录音 {_hours(recorded)} · 说话 {_hours(active)} · 有录音 {len(started_days) - len(empty_days)}/{len(started_days)} 天",
        "",
        "## 一、本周概况",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        f"| 录音分段 | {segments} 个 |",
        f"| 录音时长 | {_hours(recorded)} |",
        f"| 检测到说话 | {_hours(active)} |",
        f"| 可分析逐字稿 | {transcribed} 段 |",
        f"| 有录音的天数 | {len(started_days) - len(empty_days)} / {len(started_days)} |",
        "",
        "## 二、每日明细",
        "",
        "| 日期 | 分段 | 录音 | 说话 | 备注 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for day in days:
        if day.day > now.date():
            note = "尚未到这一天"
        elif day.segments == 0:
            note = "没有录音（本机未开机或服务未运行）"
        elif day.silent_only:
            note = "全部为静音"
        else:
            note = f"{day.transcribed} 段可分析"
        lines.append(
            f"| {day.day:%m-%d} {day.weekday} | {day.segments} 个 | {_hours(day.recorded)} | {_hours(day.active)} | {note} |"
        )

    lines += ["", "## 三、本周摘要", "", *_summary_block(summary_row, "本周", segments > 0)]

    hints: list[str] = []
    if empty_days:
        missing = "、".join(f"{day.day:%m-%d}" for day in empty_days)
        hints.append(f"**没有录音的日子：{missing}。** 这些天本机可能没有开机，报告照样保留。")
    if silent_days:
        silent = "、".join(f"{day.day:%m-%d}" for day in silent_days)
        hints.append(f"**整天空录的日子：{silent}。** 录音全部为静音，请检查麦克风。")
    if hints:
        lines += ["", "## 四、提示", "", *[f"- {hint}" for hint in hints]]
    return "\n".join(lines)


def daily_page(day: date, now: datetime | None = None) -> tuple[str, DayStats]:
    now = now or datetime.now().astimezone()
    stats = day_stats(day, now)
    summary = get_period_summary("day", _midnight(day, now.tzinfo))
    return daily_markdown(stats, summary, now), stats


def weekly_page(monday: date, now: datetime | None = None) -> tuple[str, list[DayStats]]:
    now = now or datetime.now().astimezone()
    days = [day_stats(day, now) for day in week_dates(monday)]
    summary = get_period_summary("week", _midnight(monday, now.tzinfo))
    return weekly_markdown(monday, days, summary, now), days


def recent_days(count: int, now: datetime | None = None) -> list[date]:
    today = (now or datetime.now().astimezone()).date()
    return [today - timedelta(days=offset) for offset in range(count)]


def recent_weeks(count: int, now: datetime | None = None) -> list[date]:
    monday = current_monday((now or datetime.now().astimezone()).date())
    return [monday - timedelta(weeks=offset) for offset in range(count)]


def write_book(directory: Path | None = None, days: int = 60, weeks: int = 12) -> list[Path]:
    """Write one Markdown file per day/week plus an index, so the book also exists on disk."""
    directory = directory or (ROOT / "reports")
    day_dir = directory / "日报"
    week_dir = directory / "周报"
    day_dir.mkdir(parents=True, exist_ok=True)
    week_dir.mkdir(parents=True, exist_ok=True)
    index: list[str] = ["# 录音报告书", "", "每天的日报和每周的周报，按日期倒序。", ""]

    written: list[Path] = []
    index += ["## 日报", ""]
    for day in recent_days(days):
        body, stats = daily_page(day)
        path = day_dir / f"{day:%Y-%m-%d}.md"
        path.write_text(body, encoding="utf-8")
        written.append(path)
        note = "未开机/无录音" if stats.segments == 0 else f"{stats.segments} 段 · {_hours(stats.recorded)}"
        index.append(f"- [{day:%Y-%m-%d} {stats.weekday}](日报/{day:%Y-%m-%d}.md) — {note}")

    index += ["", "## 周报", ""]
    for monday in recent_weeks(weeks):
        body, week_stats = weekly_page(monday)
        path = week_dir / f"{monday:%Y}-W{monday.isocalendar().week:02d}.md"
        path.write_text(body, encoding="utf-8")
        written.append(path)
        segments = sum(day.segments for day in week_stats)
        index.append(
            f"- [{monday:%Y-%m-%d} 起的一周（第 {monday.isocalendar().week} 周）]"
            f"(周报/{monday:%Y}-W{monday.isocalendar().week:02d}.md) — {segments} 段"
        )

    index_path = directory / "目录.md"
    index_path.write_text("\n".join(index) + "\n", encoding="utf-8")
    written.append(index_path)
    return written


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate the daily/weekly report book as Markdown files.")
    parser.add_argument("--days", type=int, default=60, help="How many daily pages to write.")
    parser.add_argument("--weeks", type=int, default=12, help="How many weekly pages to write.")
    parser.add_argument("--out", type=Path, default=None, help="Output directory (default: reports/).")
    args = parser.parse_args()
    written = write_book(args.out, days=args.days, weeks=args.weeks)
    print(f"已写出 {len(written)} 个文件到 {written[-1].parent if written else args.out}")


if __name__ == "__main__":
    main()
