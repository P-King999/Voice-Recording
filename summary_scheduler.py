from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import ROOT, settings
from storage import (
    claim_summary_job,
    complete_summary_job,
    init_db,
    get_period_summary,
    period_has_pending_segments,
    period_transcript,
    save_period_summary,
    fail_summary_job,
    save_summary_version,
)


RUNTIME_DIR = ROOT / "runtime"
RESTART_REQUEST_PATH = RUNTIME_DIR / "recorder.restart"
LOG_PATH = RUNTIME_DIR / "summary_scheduler.log"
logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass(frozen=True)
class SummaryWindow:
    key: str
    period_type: str
    start: datetime
    end: datetime
    label: str
    cutoff: datetime
    closes_recording: bool = True


def _midnight(day: datetime) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def scheduled_windows(day: datetime) -> list[SummaryWindow]:
    start = _midnight(day)
    noon = start + timedelta(hours=12)
    afternoon_end = start + timedelta(hours=17)
    evening_end = start + timedelta(hours=21)
    windows = [
        SummaryWindow(f"morning-{start:%Y-%m-%d}", "morning", start, noon, "本日上午", noon),
        SummaryWindow(f"afternoon-{start:%Y-%m-%d}", "afternoon", noon, afternoon_end, "本日下午", afternoon_end),
        SummaryWindow(f"evening-{start:%Y-%m-%d}", "evening", afternoon_end, evening_end, "本日晚上", evening_end),
        SummaryWindow(f"day-{start:%Y-%m-%d}", "day", start, evening_end, "本日", evening_end, False),
    ]
    if start.weekday() == 6:
        week_start = start - timedelta(days=6)
        windows.append(
            SummaryWindow(
                f"week-{week_start:%Y-%m-%d}", "week", week_start, evening_end,
                f"本周（{week_start:%m-%d} 至 {start:%m-%d}）", evening_end, False,
            )
        )
    return windows


def _request_recording_cutoff(window: SummaryWindow, now: datetime) -> None:
    if window.closes_recording and window.cutoff.date() == now.date() and now - window.cutoff < timedelta(minutes=2):
        RESTART_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESTART_REQUEST_PATH.touch(exist_ok=True)
        logging.info("Requested recorder cutoff for %s", window.key)


def generate_window(window: SummaryWindow, now: datetime) -> bool:
    if now < window.cutoff + timedelta(seconds=max(0, settings.summary_finalize_delay_seconds)):
        return False
    if period_has_pending_segments(window.start, window.end):
        logging.info("Waiting for %s recordings to finish transcription", window.key)
        return False
    if not claim_summary_job(window.key, window.period_type, window.start, window.end):
        return False
    try:
        existing = get_period_summary(window.period_type, window.start)
        if existing and existing["summary"]:
            complete_summary_job(window.key)
            logging.info("Kept existing %s summary", window.key)
            return False
        transcript, source_count = period_transcript(window.start, window.end)
        if transcript.strip():
            from worker import summarize

            summary, provider = summarize(transcript, window.label, settings.default_summary_mode)
        else:
            summary, provider = "\u7a7a", "system"
        save_period_summary(window.period_type, window.start, summary, provider, source_count)
        save_summary_version(window.period_type, window.start, summary, provider, source_count)
        complete_summary_job(window.key)
        logging.info("Saved %s summary with %s transcript segments", window.key, source_count)
        return True
    except Exception as exc:
        fail_summary_job(window.key, str(exc))
        logging.exception("Could not generate scheduled summary %s", window.key)
        return False


def run_due_summaries(now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    lookback = max(1, settings.summary_lookback_days)
    generated = 0
    for days_ago in range(lookback - 1, -1, -1):
        for window in scheduled_windows(now - timedelta(days=days_ago)):
            if window.cutoff > now:
                continue
            _request_recording_cutoff(window, now)
            generated += int(generate_window(window, now))
    return generated


def main() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    logging.info("Summary scheduler started; lookback=%s days", settings.summary_lookback_days)
    while True:
        try:
            run_due_summaries()
        except Exception:
            logging.exception("Scheduled summary check failed")
        time.sleep(max(20, settings.summary_scheduler_interval_seconds))


if __name__ == "__main__":
    main()
