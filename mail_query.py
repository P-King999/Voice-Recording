from __future__ import annotations

import argparse
import email
import imaplib
import json
import logging
import re
import time
from datetime import datetime
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr

from config import ROOT, settings
from reporter import build_report, send_smtp


RUNTIME_DIR = ROOT / "runtime"
STATE_PATH = RUNTIME_DIR / "mail-query-state.json"
LOG_PATH = RUNTIME_DIR / "mail-query.log"
DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})\s*(?:[-/.]|年)\s*(\d{1,2})\s*(?:[-/.]|月)\s*(\d{1,2})(?:日)?(?!\d)")

logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _load_state() -> dict[str, int]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, int]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    result: list[str] = []
    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def _message_text(message: Message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_type() != "text/plain" or part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _requested_date(text: str) -> datetime | None:
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).astimezone()
    except ValueError:
        return None


def _reply_for_request(subject: str, content: str, received_at: datetime) -> tuple[str, str]:
    target = _requested_date(f"{subject}\n{content}")
    if target:
        reply_subject, body = build_report(target.replace(hour=23, minute=59, second=59, microsecond=0))
        return f"历史{reply_subject}", body
    reply_subject, body = build_report(received_at)
    return f"当前{reply_subject}", body


def _is_authorized(message: Message) -> bool:
    sender = parseaddr(message.get("From", ""))[1].strip().lower()
    owner = (settings.report_smtp_username or settings.report_email_recipient).lower()
    is_system_report = message.get("X-Voice-Recorder-Auto-Report", "").strip() == "1"
    return bool(owner and sender == owner and not is_system_report)


def check_inbox_once() -> int:
    username = settings.report_smtp_username or settings.report_email_recipient
    if not (username and settings.report_smtp_auth_code):
        raise RuntimeError("邮件查询未配置 QQ 邮箱授权码。")
    state = _load_state()
    processed_uid = int(state.get("last_processed_uid", 0))
    with imaplib.IMAP4_SSL(settings.report_imap_host, settings.report_imap_port, timeout=30) as client:
        client.login(username, settings.report_smtp_auth_code)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法打开 QQ 邮箱收件箱。")
        status, data = client.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("无法查询 QQ 邮箱收件箱。")
        uids = [int(value) for value in data[0].split()]
        if not state and uids:
            # Do not reply to old mail when the feature is enabled for the first time.
            state["last_processed_uid"] = max(uids)
            _save_state(state)
            logging.info("邮件查询已初始化，从下一封新邮件开始处理。")
            return 0
        handled = 0
        for uid in (value for value in uids if value > processed_uid):
            status, data = client.uid("fetch", str(uid), "(RFC822)")
            if status != "OK" or not data or not data[0]:
                continue
            message = email.message_from_bytes(data[0][1])
            if _is_authorized(message):
                subject = _decode(message.get("Subject"))
                reply_subject, reply_body = _reply_for_request(subject, _message_text(message), datetime.now().astimezone())
                send_smtp(reply_subject, reply_body)
                handled += 1
                logging.info("已回复 UID %s 的报告查询。", uid)
            else:
                logging.warning("忽略未经授权或系统自动发出的邮件 UID %s。", uid)
            state["last_processed_uid"] = uid
            _save_state(state)
    return handled


def main() -> None:
    parser = argparse.ArgumentParser(description="处理 QQ 邮箱中的录音报告查询。")
    parser.add_argument("--check-once", action="store_true", help="立即检查一次收件箱。")
    args = parser.parse_args()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if args.check_once:
        check_inbox_once()
        return
    logging.info("邮件报告查询服务已启动。")
    while True:
        if settings.report_query_enabled:
            try:
                check_inbox_once()
            except Exception:
                logging.exception("邮件报告查询失败。")
        time.sleep(max(20, settings.report_query_interval_seconds))


if __name__ == "__main__":
    main()
