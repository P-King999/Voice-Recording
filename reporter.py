from __future__ import annotations

import argparse
import json
import logging
import smtplib
import ssl
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time as clock_time, timedelta
from email.message import EmailMessage

import requests

from config import ROOT, settings
from health import snapshot
from storage import (
    connect,
    feishu_archive_segments,
    feishu_daily_document,
    init_db,
    period_transcript,
    save_feishu_daily_document,
    save_feishu_record_ids,
)


RUNTIME_DIR = ROOT / "runtime"
STATE_PATH = RUNTIME_DIR / "reporter-state.json"
LOG_PATH = RUNTIME_DIR / "reporter.log"
CATCHUP_LOOKBACK = timedelta(hours=24)
CATCHUP_HINT_AFTER = timedelta(minutes=15)

logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


STATUS_LABELS = {
    "queued": "等待处理",
    "transcribing": "转写中",
    "complete": "已完成",
    "silent": "已过滤静音",
    "failed": "处理失败",
    "awaiting_dashscope_config": "等待转写配置",
}


@dataclass
class ReportWindow:
    """One report covers exactly one continuous time range, end excluded."""

    start: datetime
    end: datetime
    keys: list[str] = field(default_factory=list)
    scheduled_for: datetime | None = None
    late_by: timedelta = timedelta(0)

    @property
    def label(self) -> str:
        if self.start.date() == self.end.date():
            return f"{self.start:%m-%d %H:%M} → {self.end:%H:%M}"
        return f"{self.start:%m-%d %H:%M} → {self.end:%m-%d %H:%M}"

    @property
    def cache_key(self) -> str:
        return f"{self.start:%Y-%m-%dT%H:%M}-{self.end:%Y-%m-%dT%H:%M}"

    @property
    def is_catchup(self) -> bool:
        """Several missed triggers, or one trigger that fired much too late."""
        return len(self.keys) > 1 or self.late_by > CATCHUP_HINT_AFTER


def _load_state() -> dict[str, object]:
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, object]) -> None:
    summaries = state.get("summaries")
    if isinstance(summaries, dict) and len(summaries) > 20:
        keep = sorted(summaries)[-20:]
        state["summaries"] = {key: summaries[key] for key in keep}
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _parse_time(value: str) -> clock_time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return clock_time(hour=hour, minute=minute)


def scheduled_times() -> list[clock_time]:
    """The configured report triggers, sorted, ignoring malformed entries."""
    result: list[clock_time] = []
    for value in settings.report_times:
        try:
            result.append(_parse_time(value))
        except ValueError:
            logging.error("Ignoring invalid REPORT_TIMES value: %s", value)
    return sorted(set(result))


def _key(instant: datetime) -> str:
    return f"{instant:%Y-%m-%d-%H:%M}"


def _instant_from_key(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d-%H:%M").astimezone()
    except (ValueError, TypeError):
        return None


def window_for_slot(slot: datetime, slots: list[clock_time] | None = None) -> ReportWindow:
    """A trigger at HH:MM reports everything since the previous trigger.

    The first trigger of a day starts at the previous day's last trigger, so a
    machine that was off overnight still reports that audio in the morning.
    """
    slots = slots or scheduled_times()
    if not slots:
        day_start = slot.replace(hour=0, minute=0, second=0, microsecond=0)
        return ReportWindow(day_start, slot, [_key(slot)], slot)
    earlier = [value for value in slots if value < slot.time()]
    if earlier:
        start = slot.replace(hour=earlier[-1].hour, minute=earlier[-1].minute, second=0, microsecond=0)
    else:
        previous_day = slot - timedelta(days=1)
        last = slots[-1]
        start = previous_day.replace(hour=last.hour, minute=last.minute, second=0, microsecond=0)
    return ReportWindow(start, slot, [_key(slot)], slot)


def today_window(now: datetime) -> ReportWindow:
    """Ad-hoc reports (mail queries, manual sends) cover the current calendar day."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return ReportWindow(start, now, [], None)


def pending_report(
    now: datetime, state: dict[str, object], slots: list[clock_time] | None = None
) -> ReportWindow | None:
    """Return the range that still has to be reported, merging missed triggers.

    Reports are ranges, not fixed calendar buckets: consecutive missed triggers
    produce a single catch-up report so no audio silently disappears.
    """
    slots = slots or scheduled_times()
    if not slots:
        return None
    last_key = str(state.get("last_report") or "")
    due: list[tuple[str, datetime]] = []
    for days_ago in (2, 1, 0):
        day = (now - timedelta(days=days_ago)).replace(hour=0, minute=0, second=0, microsecond=0)
        for value in slots:
            instant = day.replace(hour=value.hour, minute=value.minute)
            if instant <= now and _key(instant) > last_key:
                due.append((_key(instant), instant))
    due.sort()
    if not due:
        return None

    first_start = window_for_slot(due[0][1], slots).start
    previous_end = _instant_from_key(last_key)
    if previous_end is None:
        start = max(first_start, now - CATCHUP_LOOKBACK)
    else:
        start = max(first_start, previous_end)
    end = min(due[-1][1], now)
    if end <= start:
        return None
    return ReportWindow(start, end, [key for key, _ in due], due[-1][1], now - due[-1][1])


def _local_rows(start: datetime, end: datetime) -> list[dict[str, object]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT recorded_at, duration_seconds, status, active_seconds, error FROM segments ORDER BY recorded_at"
        ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        recorded_at = datetime.fromisoformat(str(row["recorded_at"])).astimezone()
        if start <= recorded_at < end:
            result.append(dict(row))
    return result


def _recorder_log_lines(day: datetime) -> list[str]:
    recorder_log = LOG_PATH.with_name("recorder.log")
    if not recorder_log.exists():
        return []
    prefix = day.strftime("%Y-%m-%d")
    try:
        content = recorder_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in content.splitlines() if line.startswith(prefix)]


def _latest_event_text(lines: list[str]) -> str:
    if not lines:
        return "今天没有录音器日志记录"
    line = lines[-1]
    if "keeping the current file open" in line:
        return "录音出现过音频缓冲溢出，少量帧被丢弃，但录音文件保持打开、没有中断。"
    if "input overflow" in line:
        return "录音输入缓冲溢出，录音器已重启（音频设备可能不稳定）。"
    if "Audio writer started" in line:
        return "录音写入器已启动，正在持续保存音频。"
    if "Audio callback started" in line:
        return "音频输入回调已启动。"
    if "Recovered interrupted" in line:
        return "上次留下的中断录音已自动修复并入库。"
    if "Saved " in line:
        return "已完成并保存一个录音分段。"
    if "Recording " in line:
        return "正在录制新的音频分段。"
    if "WARNING" in line or "ERROR" in line or "CRITICAL" in line:
        return f"检测到录音日志异常：{line}"
    return "录音器正在运行。"


def _duration_text(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 1:
        return f"{max(0.0, seconds):.0f} 秒"
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def _status_text(statuses: Counter[str]) -> str:
    if not statuses:
        return "没有分段"
    return "，".join(f"{STATUS_LABELS.get(name, name)} {count} 个" for name, count in sorted(statuses.items()))


def _window_summary(window: ReportWindow, state: dict[str, object]) -> tuple[str | None, str | None]:
    cache = state.get("summaries")
    cached = cache.get(window.cache_key) if isinstance(cache, dict) else None
    if isinstance(cached, dict) and cached.get("text"):
        return str(cached["text"]), str(cached.get("provider") or "") or None
    transcript, _ = period_transcript(window.start, window.end)
    if not transcript.strip():
        return None, None
    try:
        from worker import summarize

        text, provider = summarize(transcript, f"{window.start:%m-%d %H:%M} 至 {window.end:%m-%d %H:%M}")
    except Exception as exc:
        logging.exception("Could not summarize report window")
        return f"本时段的摘要生成失败：{exc}", None
    if isinstance(cache, dict):
        cache[window.cache_key] = {"text": text, "provider": provider}
    else:
        state["summaries"] = {window.cache_key: {"text": text, "provider": provider}}
    return text, provider


def build_report(
    now: datetime | None = None,
    scheduled_time: clock_time | None = None,
    window: ReportWindow | None = None,
    state: dict[str, object] | None = None,
) -> tuple[str, str]:
    """Build one report for exactly one continuous range.

    Statistics, summary and headings all describe the same range, which is the
    part the earlier reports left ambiguous.
    """
    now = now or datetime.now().astimezone()
    state = state if state is not None else {}
    if window is None:
        if scheduled_time is None:
            window = today_window(now)
        else:
            slot = now.replace(hour=scheduled_time.hour, minute=scheduled_time.minute, second=0, microsecond=0)
            window = window_for_slot(slot)

    rows = _local_rows(window.start, window.end)
    statuses = Counter(str(row["status"]) for row in rows)
    active_seconds = sum(float(row["active_seconds"] or 0) for row in rows)
    recorded_seconds = sum(float(row["duration_seconds"] or 0) for row in rows)
    transcribed = sum(1 for row in rows if str(row["status"]) == "complete")

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_rows = rows if window.start >= today_start else _local_rows(today_start, now)
    today_active = sum(float(row["active_seconds"] or 0) for row in today_rows)
    today_recorded = sum(float(row["duration_seconds"] or 0) for row in today_rows)

    service_state = snapshot(ROOT)
    services_ok = all(bool(value) for value in service_state["services"].values())
    last_event = _latest_event_text(_recorder_log_lines(now))
    summary, provider = _window_summary(window, state)

    hints: list[str] = []
    if not services_ok:
        offline = [name for name, alive in service_state["services"].items() if not alive]
        hints.append(f"后台服务未全部运行（{'、'.join(offline)}），报告数据可能不完整。")
    if not rows:
        hints.append("本时段没有新的录音分段：如果当时电脑开着，说明录音服务没有运行（关机期间的录音不会被记录）。")
    elif active_seconds == 0:
        hints.append("本时段的录音全部没有检测到声音：请检查麦克风是否选错（例如选中了虚拟声卡）或被系统静音。")
    if window.is_catchup:
        if window.scheduled_for and len(window.keys) > 1:
            planned = "、".join(key[-5:] for key in window.keys)
            hints.append(f"本报告为补发：{planned} 这几个时段没有按时发送，已合并到本次。")
        elif window.scheduled_for:
            hints.append(
                f"本报告为补发：计划 {window.scheduled_for:%m-%d %H:%M} 发送，实际 {now:%m-%d %H:%M}，"
                "本机当时可能未开机或程序未运行。"
            )
        else:
            hints.append("本报告为补发，包含之前未发送的时段。")

    lines = [
        f"录音日报 · {now:%Y-%m-%d %H:%M}",
        "",
        "【本次报告时段】",
        f"{window.label}（共 {_duration_text((window.end - window.start).total_seconds())}）",
        "",
        "【本时段录音】",
        f"- 新增分段：{len(rows)} 个",
        f"- 录音时长：{_duration_text(recorded_seconds)}",
        f"- 检测到说话：{_duration_text(active_seconds)}",
        f"- 处理状态：{_status_text(statuses)}",
        f"- 可分析逐字稿：{transcribed} 段",
        "",
        "【本时段摘要】",
        summary or "本时段没有可用的逐字稿，因此没有生成内容摘要。",
    ]
    if provider:
        lines.append(f"- 摘要模型：{provider}")
    lines += [
        "",
        f"【今日累计】{today_start:%m-%d} 00:00 → {now:%m-%d %H:%M}",
        f"- 分段 {len(today_rows)} 个，录音 {_duration_text(today_recorded)}，说话 {_duration_text(today_active)}",
        "",
        "【服务状态】",
        f"- 后台服务：{'正常' if services_ok else '需要检查'}",
        f"- 数据库：{'正常' if service_state['database_ok'] else service_state['database_error']}",
        f"- 剩余磁盘：{float(service_state['free_gb']):.1f} GB",
        f"- 最近录音事件：{last_event}",
    ]
    if hints:
        lines += ["", "【提示】", *[f"- {hint}" for hint in hints]]

    subject = f"录音日报 {now:%m-%d %H:%M}（覆盖 {window.label}）"
    return subject, "\n".join(lines)


def send_smtp(subject: str, body: str) -> None:
    username = settings.report_smtp_username or settings.report_email_recipient
    if not (settings.report_email_recipient and username and settings.report_smtp_auth_code):
        raise RuntimeError("QQ SMTP is not configured: set REPORT_SMTP_AUTH_CODE (and optionally REPORT_SMTP_USERNAME).")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = settings.report_email_recipient
    # The inbox query service uses this marker to avoid replying to reports it sent itself.
    message["X-Voice-Recorder-Auto-Report"] = "1"
    message.set_content(body)
    context = ssl.create_default_context()
    if settings.report_smtp_port == 465:
        with smtplib.SMTP_SSL(settings.report_smtp_host, settings.report_smtp_port, context=context, timeout=30) as client:
            client.login(username, settings.report_smtp_auth_code)
            client.send_message(message)
    else:
        with smtplib.SMTP(settings.report_smtp_host, settings.report_smtp_port, timeout=30) as client:
            client.starttls(context=context)
            client.login(username, settings.report_smtp_auth_code)
            client.send_message(message)


def send_wecom(subject: str, body: str) -> None:
    if not settings.report_wecom_webhook:
        raise RuntimeError("WeCom is not configured: set REPORT_WECOM_WEBHOOK.")
    response = requests.post(
        settings.report_wecom_webhook,
        json={"msgtype": "markdown", "markdown": {"content": f"## {subject}\n{body}"}},
        timeout=30,
    )
    response.raise_for_status()
    if response.json().get("errcode", 0) != 0:
        raise RuntimeError(f"WeCom rejected the report: {response.text}")


def send_pushplus(subject: str, body: str) -> None:
    if not settings.report_pushplus_token:
        raise RuntimeError("PushPlus is not configured: set REPORT_PUSHPLUS_TOKEN.")
    response = requests.post(
        "https://www.pushplus.plus/send",
        json={"token": settings.report_pushplus_token, "title": subject, "content": body, "template": "txt"},
        timeout=30,
    )
    response.raise_for_status()
    if response.json().get("code") != 200:
        raise RuntimeError(f"PushPlus rejected the report: {response.text}")


def send_feishu(subject: str, body: str) -> None:
    from feishu import append_daily_document, chat_id_for_name, create_daily_document, report_card, sync_bitable_archive, tenant_access_token

    token = tenant_access_token(settings.feishu_app_id, settings.feishu_app_secret)
    created = sync_bitable_archive(
        token, settings.feishu_bitable_app_token, settings.feishu_bitable_table_id, feishu_archive_segments()
    )
    save_feishu_record_ids(created)
    now = datetime.now().astimezone()
    day = now.date().isoformat()
    document = feishu_daily_document(day)
    if document:
        document_id, document_url = str(document["document_id"]), str(document["document_url"])
    else:
        document_id, document_url = create_daily_document(token, f"录音日报 - {day}")
        save_feishu_daily_document(day, document_id, document_url)
    append_daily_document(token, document_id, subject, body)
    chat_id = chat_id_for_name(token, settings.feishu_chat_name)
    card = report_card(subject, body)
    card["elements"].append(
        {
            "tag": "action",
            "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "浏览录音档案"}, "type": "primary", "url": f"https://feishu.cn/base/{settings.feishu_bitable_app_token}"},
                {"tag": "button", "text": {"tag": "plain_text", "content": "打开今日日报"}, "type": "default", "url": document_url},
            ],
        }
    )
    from feishu import _api_request

    _api_request(
        token,
        "POST",
        "/im/v1/messages",
        "send Feishu report",
        params={"receive_id_type": "chat_id"},
        json={"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
    )


def deliver_report(window: ReportWindow, state: dict[str, object] | None = None) -> None:
    """Deliver one report. Raises only when every configured channel failed."""
    subject, body = build_report(window=window, state=state)
    handlers = {"smtp": send_smtp, "wecom": send_wecom, "pushplus": send_pushplus, "feishu": send_feishu}
    errors: list[str] = []
    delivered = 0
    for channel in settings.report_channels:
        handler = handlers.get(channel)
        if handler is None:
            errors.append(f"Unsupported report channel: {channel}")
            continue
        try:
            handler(subject, body)
            delivered += 1
            logging.info("Delivered report through %s for %s (%s)", channel, window.label, ",".join(window.keys) or "ad-hoc")
        except Exception as exc:
            errors.append(f"{channel}: {exc}")
            logging.exception("Could not deliver report through %s", channel)
    if errors and delivered == 0:
        raise RuntimeError("; ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description="Send local Voice Recorder status reports.")
    parser.add_argument("--send-now", action="store_true", help="Send one report immediately.")
    parser.add_argument("--send-feishu-test", action="store_true", help="Send a Feishu connection test without generating a report.")
    parser.add_argument("--sync-feishu-archive", action="store_true", help="Sync local recording metadata to the configured Feishu Bitable.")
    args = parser.parse_args()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    if args.send_feishu_test:
        send_feishu(
            "录音日报机器人连接成功",
            f"已于 {datetime.now().astimezone():%Y-%m-%d %H:%M} 完成连接测试。\n\n后续日报会按 REPORT_TIMES 自动发送到此群。",
        )
        return
    if args.sync_feishu_archive:
        from feishu import sync_bitable_archive, tenant_access_token

        token = tenant_access_token(settings.feishu_app_id, settings.feishu_app_secret)
        created = sync_bitable_archive(token, settings.feishu_bitable_app_token, settings.feishu_bitable_table_id, feishu_archive_segments())
        save_feishu_record_ids(created)
        return
    if args.send_now:
        state = _load_state()
        deliver_report(today_window(datetime.now().astimezone()), state)
        _save_state(state)
        return
    state = _load_state()
    logging.info("Reporter started. Schedule: %s", ", ".join(settings.report_times))
    while True:
        if settings.report_enabled:
            now = datetime.now().astimezone()
            window = pending_report(now, state)
            if window:
                try:
                    deliver_report(window, state)
                except Exception:
                    logging.exception("Scheduled report was not delivered; it will be retried")
                else:
                    state["last_report"] = window.keys[-1]
                _save_state(state)
        time.sleep(20)


if __name__ == "__main__":
    main()
