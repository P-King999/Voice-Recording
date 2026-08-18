from __future__ import annotations

import argparse
import json
import logging
import smtplib
import ssl
import time
from collections import Counter
from datetime import datetime, time as clock_time, timedelta
from email.message import EmailMessage

import requests

from config import ROOT, settings
from health import snapshot
from storage import (
    connect,
    feishu_archive_segments,
    feishu_daily_document,
    get_period_summary,
    init_db,
    period_transcript,
    save_feishu_daily_document,
    save_feishu_record_ids,
)


RUNTIME_DIR = ROOT / "runtime"
STATE_PATH = RUNTIME_DIR / "reporter-state.json"
LOG_PATH = RUNTIME_DIR / "reporter.log"

logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _load_state() -> dict[str, str]:
    try:
        loaded = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _parse_time(value: str) -> clock_time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return clock_time(hour=hour, minute=minute)


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
    return [
        line.strip()
        for line in recorder_log.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith(prefix)
    ]


def _latest_event_text(lines: list[str]) -> str:
    if not lines:
        return "今天没有录音器日志记录"
    line = lines[-1]
    if "Audio writer started" in line:
        return "录音写入器已启动，正在持续保存音频。"
    if "Audio callback started" in line:
        return "音频输入回调已启动。"
    if "Saved " in line:
        return "已完成并保存一个录音分段。"
    if "Recording " in line:
        return "正在录制新的音频分段。"
    if "WARNING" in line or "ERROR" in line or "CRITICAL" in line:
        return f"检测到录音日志异常：{line}"
    return "录音器正在运行。"


STATUS_LABELS = {
    "queued": "等待处理",
    "transcribing": "转写中",
    "complete": "已完成",
    "silent": "已过滤静音",
    "failed": "处理失败",
    "awaiting_dashscope_config": "等待转写配置",
}


def _period_summary(period_type: str, start: datetime, end: datetime, label: str) -> tuple[str, str | None]:
    transcript, source_count = period_transcript(start, end)
    existing = get_period_summary(period_type, start)
    if existing and existing["summary"]:
        return str(existing["summary"]), str(existing["provider"])

    if not transcript:
        return f"{label}没有可用的逐字稿，因此未生成内容摘要。", None
    try:
        # Generate only when the dashboard has not already saved the same period summary.
        from worker import generate_period_summary

        summary, provider = generate_period_summary(period_type, start, end, label)
        logging.info("已为定时报告生成%s摘要，来源分段数：%s", label, source_count)
        return summary, provider
    except Exception as exc:
        logging.exception("无法为定时报告生成%s摘要", label)
        return f"{label}已有 {source_count} 个可用逐字稿分段，但摘要生成失败：{exc}", None


def report_summary_window(now: datetime, scheduled_time: clock_time | None = None) -> tuple[str, datetime, datetime]:
    """Choose a report window.

    Scheduled reports use their configured trigger time, rather than the few
    seconds later when the polling loop happens to run. Ad-hoc reports retain
    the current-time behavior used by the mail-query feature.
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if scheduled_time is not None:
        if scheduled_time < clock_time(17):
            return "morning", day_start, day_start + timedelta(hours=12)
        if scheduled_time < clock_time(21):
            return "afternoon", day_start + timedelta(hours=12), day_start + timedelta(hours=17)
        return "day", day_start, day_start + timedelta(hours=21)
    if now.time() < clock_time(12, 10):
        return "morning", day_start, day_start + timedelta(hours=12)
    if now.time() < clock_time(17, 10):
        return "afternoon", day_start + timedelta(hours=12), day_start + timedelta(hours=17)
    if now.time() < clock_time(21, 10):
        return "evening", day_start + timedelta(hours=17), day_start + timedelta(hours=21)
    return "day", day_start, day_start + timedelta(hours=21)


def build_report(now: datetime | None = None, scheduled_time: clock_time | None = None) -> tuple[str, str]:
    now = now or datetime.now().astimezone()
    summary_type, report_start, report_end = report_summary_window(now, scheduled_time)
    period_labels = {"morning": "本日上午", "afternoon": "本日下午", "evening": "本日晚上", "day": "本日"}
    period_label = period_labels[summary_type]
    period_heading = f"{period_label}录音（{report_start:%H:%M}-{report_end:%H:%M}）"
    period_rows = _local_rows(report_start, report_end)
    statuses = Counter(str(row["status"]) for row in period_rows)
    active_seconds = sum(float(row["active_seconds"] or 0) for row in period_rows)
    recorded_seconds = sum(float(row["duration_seconds"] or 0) for row in period_rows)
    service_state = snapshot(ROOT)
    recorder_lines = _recorder_log_lines(now)
    last_event = _latest_event_text(recorder_lines)
    services_ok = all(bool(value) for value in service_state["services"].values())
    summary, provider = _period_summary(summary_type, report_start, report_end, period_label)
    status_text = "，".join(f"{STATUS_LABELS.get(name, name)} {count}" for name, count in sorted(statuses.items()))

    lines = [
        f"生成时间：{now:%Y-%m-%d %H:%M}",
        "",
        period_heading,
        f"- 已完成分段：{len(period_rows)} 个；总录音时长：{recorded_seconds / 3600:.1f} 小时。",
        "- 处理状态：" + (status_text or "没有已完成分段"),
        f"- 检测到的活跃语音：{active_seconds:.1f} 秒。",
        "- 被识别为静音的录音不会包含逐字稿。",
        "",
        f"{period_label}摘要",
        summary,
        *([f"- 摘要模型：{provider}。"] if provider else []),
        "",
        "录音服务状态",
        f"- 后台服务：{'正常' if services_ok else '需要检查'}。",
        f"- 数据库：{'正常' if service_state['database_ok'] else service_state['database_error']}。",
        f"- 剩余磁盘空间：{float(service_state['free_gb']):.1f} GB。",
        f"- 最新录音事件：{last_event}",
        "",
        "今日截至目前",
        f"- 已完成分段：{len(period_rows)} 个。",
    ]
    if period_rows and active_seconds == 0:
        lines.append(f"- {period_label}录音已正常保存，但没有检测到超过当前阈值的语音。")
    if not period_rows:
        lines.append(f"- 本次报告生成时，尚无已完成的{period_label}录音分段。")
    return f"录音日报 - {now:%Y-%m-%d}", "\n".join(lines)


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


def deliver_report(scheduled_time: clock_time | None = None) -> None:
    subject, body = build_report(scheduled_time=scheduled_time)
    handlers = {"smtp": send_smtp, "wecom": send_wecom, "pushplus": send_pushplus, "feishu": send_feishu}
    errors: list[str] = []
    for channel in settings.report_channels:
        handler = handlers.get(channel)
        if handler is None:
            errors.append(f"Unsupported report channel: {channel}")
            continue
        try:
            handler(subject, body)
            logging.info("Delivered report through %s", channel)
        except Exception as exc:
            errors.append(f"{channel}: {exc}")
            logging.exception("Could not deliver report through %s", channel)
    if errors:
        raise RuntimeError("; ".join(errors))


def due_schedule(now: datetime, state: dict[str, str]) -> tuple[str, clock_time] | None:
    latest: tuple[str, clock_time] | None = None
    latest_time: clock_time | None = None
    for scheduled in settings.report_times:
        try:
            scheduled_time = _parse_time(scheduled)
        except ValueError:
            logging.error("Ignoring invalid REPORT_TIMES value: %s", scheduled)
            continue
        report_key = f"{now.date().isoformat()}-{scheduled}"
        if now.time() >= scheduled_time and (latest_time is None or scheduled_time > latest_time):
            latest, latest_time = (report_key, scheduled_time), scheduled_time
    return latest if latest and state.get("last_report") != latest[0] else None


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
        deliver_report()
        return
    state = _load_state()
    logging.info("Reporter started. Schedule: %s", ", ".join(settings.report_times))
    while True:
        if settings.report_enabled:
            now = datetime.now().astimezone()
            due = due_schedule(now, state)
            if due:
                report_key, scheduled_time = due
                try:
                    deliver_report(scheduled_time)
                except Exception:
                    logging.exception("Scheduled report was not delivered")
                state["last_report"] = report_key
                _save_state(state)
        time.sleep(20)


if __name__ == "__main__":
    main()
