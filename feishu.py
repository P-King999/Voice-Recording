from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import requests


API_BASE = "https://open.feishu.cn/open-apis"
REQUEST_TIMEOUT_SECONDS = 30
CARD_CHUNK_SIZE = 3_500
MAX_REPORT_CHARS = 14_000


class FeishuError(RuntimeError):
    """An Open Platform request was rejected or could not be completed."""


def _response_json(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuError(f"Feishu {action} returned invalid JSON (HTTP {response.status_code}).") from exc
    if response.status_code >= 400 or payload.get("code", 0) != 0:
        message = payload.get("msg") or response.text[:500]
        raise FeishuError(f"Feishu {action} failed: {message}")
    return payload


def tenant_access_token(app_id: str, app_secret: str) -> str:
    if not app_id or not app_secret:
        raise FeishuError("Feishu is not configured: set FEISHU_APP_ID and FEISHU_APP_SECRET.")
    try:
        response = requests.post(
            f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise FeishuError(f"Could not get a Feishu tenant access token: {exc}") from exc
    payload = _response_json(response, "tenant token request")
    token = payload.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise FeishuError("Feishu tenant token response did not contain tenant_access_token.")
    return token


def _list_chats(token: str) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    chats: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            response = requests.get(
                f"{API_BASE}/im/v1/chats",
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise FeishuError(f"Could not list Feishu chats: {exc}") from exc
        payload = _response_json(response, "chat list request")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        chats.extend(item for item in items if isinstance(item, dict))
        if not data.get("has_more"):
            return chats
        page_token = data.get("page_token")
        if not isinstance(page_token, str) or not page_token:
            raise FeishuError("Feishu chat list returned has_more without a page token.")


def chat_id_for_name(token: str, chat_name: str) -> str:
    normalized_name = chat_name.strip()
    if not normalized_name:
        raise FeishuError("Feishu is not configured: set FEISHU_CHAT_NAME to the group containing the bot.")
    matches = [chat for chat in _list_chats(token) if str(chat.get("name", "")).strip() == normalized_name]
    if not matches:
        raise FeishuError(
            f"No chat named '{normalized_name}' was visible to the bot. Add the bot to that group and grant the chat-read permission."
        )
    if len(matches) > 1:
        raise FeishuError(f"More than one chat is named '{normalized_name}'. Rename the notification group to a unique name.")
    chat_id = matches[0].get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise FeishuError(f"Feishu returned no chat ID for '{normalized_name}'.")
    return chat_id


def _markdown_chunks(body: str) -> list[str]:
    normalized = body.strip()
    if len(normalized) > MAX_REPORT_CHARS:
        normalized = normalized[:MAX_REPORT_CHARS].rsplit("\n", 1)[0].rstrip()
        normalized += "\n\n> 日报内容过长，已截断。完整档案后续可在飞书日报文档中查看。"
    if not normalized:
        return ["暂无报告内容。"]

    chunks: list[str] = []
    current = ""
    for line in normalized.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= CARD_CHUNK_SIZE:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > CARD_CHUNK_SIZE:
            chunks.append(line[:CARD_CHUNK_SIZE])
            line = line[CARD_CHUNK_SIZE:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def report_card(subject: str, body: str) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for index, chunk in enumerate(_markdown_chunks(body)):
        if index:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": chunk}})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": subject},
            "template": "blue",
        },
        "elements": elements,
    }


def send_report(app_id: str, app_secret: str, chat_name: str, subject: str, body: str) -> None:
    token = tenant_access_token(app_id, app_secret)
    chat_id = chat_id_for_name(token, chat_name)
    try:
        response = requests.post(
            f"{API_BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(report_card(subject, body), ensure_ascii=False),
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise FeishuError(f"Could not send the Feishu report: {exc}") from exc
    _response_json(response, "message send request")


def _api_request(token: str, method: str, endpoint: str, action: str, **kwargs: Any) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(kwargs.pop("headers", {}))
    try:
        response = requests.request(
            method,
            f"{API_BASE}{endpoint}",
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise FeishuError(f"Could not {action}: {exc}") from exc
    return _response_json(response, action)


ARCHIVE_FIELDS: tuple[tuple[str, int], ...] = (
    ("本地 ID", 1),
    ("录音时间", 5),
    ("录音时间（精确）", 1),
    ("状态", 1),
    ("星级", 2),
    ("时长（分钟）", 2),
    ("活跃语音（秒）", 2),
    ("摘要", 1),
    ("对话概览", 1),
    ("关键人物", 1),
    ("话题", 1),
    ("重要事件", 1),
    ("待办", 1),
    ("重点原话", 1),
    ("完整对话分析", 1),
    ("标签", 1),
    ("收藏", 7),
    ("备注", 1),
    ("逐字稿", 1),
    ("本地音频路径", 1),
    ("同步时间", 5),
)


def _table_fields(token: str, app_token: str, table_id: str) -> list[dict[str, Any]]:
    payload = _api_request(token, "GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields", "list Bitable fields")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return [item for item in data.get("items", []) if isinstance(item, dict)]


def _ensure_archive_fields(token: str, app_token: str, table_id: str) -> str:
    fields = _table_fields(token, app_token, table_id)
    primary = next((field for field in fields if field.get("is_primary")), fields[0] if fields else None)
    if not primary or not isinstance(primary.get("field_name"), str):
        raise FeishuError("The selected Bitable has no primary field.")
    existing = {str(field.get("field_name")) for field in fields}
    for field_name, field_type in ARCHIVE_FIELDS:
        if field_name in existing:
            continue
        _api_request(
            token,
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            f"create Bitable field '{field_name}'",
            json={"field_name": field_name, "type": field_type},
        )
    return str(primary["field_name"])


def _analysis(record: Any) -> dict[str, Any]:
    try:
        analysis = json.loads(str(record["conversation_analysis"] or ""))
    except (TypeError, ValueError):
        return {}
    return analysis if isinstance(analysis, dict) else {}


def _analysis_value(analysis: dict[str, Any], key: str, default: str = "") -> str:
    value = analysis.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _object_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _analysis_projection(analysis: dict[str, Any]) -> dict[str, str]:
    people = []
    for person in _object_list(analysis.get("key_people")):
        name = _analysis_value(person, "name_or_label")
        background = _analysis_value(person, "background")
        relationships = []
        for relationship in _object_list(person.get("relationships")):
            other = _analysis_value(relationship, "with")
            relation = _analysis_value(relationship, "relationship")
            if other or relation:
                relationships.append(f"与 {other}：{relation}".strip("："))
        details = "；".join(part for part in [background, *relationships] if part)
        if name:
            people.append(f"{name}（{details}）" if details else name)

    topics, actions = [], []
    for topic in _object_list(analysis.get("topics")):
        title = _analysis_value(topic, "title")
        summary = _analysis_value(topic, "summary")
        outcome = _analysis_value(topic, "outcome")
        if title or summary or outcome:
            topics.append("：".join(part for part in [title, summary, f"结论：{outcome}" if outcome else ""] if part))
        for item in topic.get("action_items", []) if isinstance(topic.get("action_items"), list) else []:
            if isinstance(item, dict):
                task = _analysis_value(item, "task")
                owner = _analysis_value(item, "owner", "未明确")
                due_date = _analysis_value(item, "due_date", "未明确")
                if task:
                    actions.append(f"{task}（负责人：{owner}；截止：{due_date}）")
            elif isinstance(item, str) and item.strip():
                actions.append(item.strip())

    events = []
    for event in _object_list(analysis.get("noteworthy_events")):
        title = _analysis_value(event, "title")
        details = _analysis_value(event, "details")
        if title or details:
            events.append("：".join(part for part in [title, details] if part))

    overview = _analysis_value(analysis, "overview")
    quotes = _string_list(analysis.get("highlight_quotes"))
    detailed_lines = [
        f"概览：{overview}" if overview else "",
        f"关键人物：{'；'.join(people)}" if people else "",
        f"话题：{'；'.join(topics)}" if topics else "",
        f"重要事件：{'；'.join(events)}" if events else "",
        f"待办：{'；'.join(actions)}" if actions else "",
        f"重点原话：{'；'.join(quotes)}" if quotes else "",
    ]
    return {
        "overview": overview,
        "people": "；".join(people),
        "topics": "；".join(topics),
        "events": "；".join(events),
        "actions": "；".join(actions),
        "quotes": "；".join(quotes),
        "detailed": "\n".join(line for line in detailed_lines if line),
    }


def _archive_fields(record: Any, title_field: str) -> dict[str, Any]:
    recorded_at = datetime.fromisoformat(str(record["recorded_at"])).astimezone()
    analysis = _analysis(record)
    projection = _analysis_projection(analysis)
    title = _analysis_value(analysis, "title") or projection["overview"] or f"录音 {recorded_at:%Y-%m-%d %H:%M}"
    try:
        tags = ", ".join(json.loads(str(record["tags"] or "[]")))
    except (TypeError, ValueError):
        tags = ""
    return {
        title_field: title[:500],
        "本地 ID": str(record["id"]),
        "录音时间": int(recorded_at.timestamp() * 1000),
        "录音时间（精确）": recorded_at.strftime("%Y-%m-%d %H:%M:%S"),
        "状态": str(record["status"]),
        "星级": int(record["excitement_rating"] or 0),
        "时长（分钟）": round(float(record["duration_seconds"] or 0) / 60, 1),
        "活跃语音（秒）": round(float(record["active_seconds"] or 0), 1),
        "摘要": (projection["overview"] or str(record["summary"] or ""))[:10_000],
        "对话概览": projection["overview"][:10_000],
        "关键人物": projection["people"][:10_000],
        "话题": projection["topics"][:10_000],
        "重要事件": projection["events"][:10_000],
        "待办": projection["actions"][:10_000],
        "重点原话": projection["quotes"][:10_000],
        "完整对话分析": projection["detailed"][:20_000],
        "标签": tags,
        "收藏": bool(record["favorite"]),
        "备注": str(record["notes"] or ""),
        "逐字稿": str(record["transcript"] or "")[:50_000],
        "本地音频路径": str(record["path"]),
        "同步时间": int(datetime.now().astimezone().timestamp() * 1000),
    }


def sync_bitable_archive(token: str, app_token: str, table_id: str, records: list[Any]) -> list[tuple[str, int]]:
    if not app_token or not table_id:
        raise FeishuError("Set FEISHU_BITABLE_APP_TOKEN and FEISHU_BITABLE_TABLE_ID before syncing the archive.")
    title_field = _ensure_archive_fields(token, app_token, table_id)
    created: list[tuple[str, int]] = []
    for start in range(0, len(records), 100):
        batch = records[start:start + 100]
        new_records = [record for record in batch if not record["feishu_record_id"]]
        existing_records = [record for record in batch if record["feishu_record_id"]]
        if new_records:
            payload = _api_request(
                token,
                "POST",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
                "create Bitable records",
                json={"records": [{"fields": _archive_fields(record, title_field)} for record in new_records]},
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            remote_records = data.get("records") if isinstance(data.get("records"), list) else []
            if len(remote_records) != len(new_records):
                raise FeishuError("Bitable did not return every newly created record ID.")
            for remote, local in zip(remote_records, new_records):
                record_id = remote.get("record_id") if isinstance(remote, dict) else None
                if not isinstance(record_id, str) or not record_id:
                    raise FeishuError("Bitable returned an invalid record ID.")
                created.append((record_id, int(local["id"])))
        if existing_records:
            _api_request(
                token,
                "POST",
                f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
                "update Bitable records",
                json={
                    "records": [
                        {"record_id": str(record["feishu_record_id"]), "fields": _archive_fields(record, title_field)}
                        for record in existing_records
                    ]
                },
            )
    return created


def create_daily_document(token: str, title: str) -> tuple[str, str]:
    payload = _api_request(token, "POST", "/docx/v1/documents", "create daily document", json={"title": title})
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    document = data.get("document") if isinstance(data.get("document"), dict) else {}
    document_id = document.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        raise FeishuError("Feishu did not return a document ID for the daily report.")
    return document_id, f"https://feishu.cn/docx/{document_id}"


def append_daily_document(token: str, document_id: str, heading: str, body: str) -> None:
    paragraphs = [heading, *_markdown_chunks(body)]
    children = [
        {
            "block_type": 2,
            "text": {"elements": [{"text_run": {"content": paragraph, "text_element_style": {}}}], "style": {}},
        }
        for paragraph in paragraphs
    ]
    _api_request(
        token,
        "POST",
        f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
        "append daily document content",
        json={"children": children},
    )
