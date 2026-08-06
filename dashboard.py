from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path

import streamlit as st

from config import DB_PATH, ensure_storage, settings
from storage import dashboard_segments, get_period_summary, init_db, period_transcript, update_segment


STATUS_LABELS = {
    "queued": "等待处理",
    "awaiting_dashscope_config": "等待百炼配置",
    "transcribing": "百炼转写中",
    "complete": "已完成",
    "silent": "已过滤静音",
    "failed": "处理失败",
    "realtime_failed": "旧实时识别失败",
    "realtime_unavailable": "旧片段无法补发实时识别",
}
SUMMARY_MODE_LABELS = {
    "deepseek_v4_flash": "DeepSeek-V4-Flash",
    "deepseek_v4_pro": "DeepSeek-V4-Pro",
    "qwen_plus": "千问 Qwen Plus",
}


def format_offset(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def split_transcript(transcript: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", transcript).strip()
    sentences = [sentence.strip() for sentence in re.split(r"(?<=[。！？!?；;])\s*", normalized) if sentence.strip()]
    readable: list[str] = []
    for sentence in sentences:
        if len(sentence) <= 64:
            readable.append(sentence)
            continue
        readable.extend(part.strip() for part in re.findall(r".{1,64}(?:[，,、]|$)", sentence) if part.strip())
    return readable or [normalized]


def transcript_timeline(raw_timeline: str | None) -> list[dict[str, object]]:
    if not raw_timeline:
        return []
    try:
        items = json.loads(raw_timeline)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    timeline: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            start_seconds = float(item["start_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        if start_seconds < 0 or not item["text"].strip():
            continue
        timeline.append({**item, "start_seconds": start_seconds, "text": item["text"].strip()})
    return sorted(timeline, key=lambda item: float(item["start_seconds"]))


def parse_conversation_analysis(raw_analysis: str | None) -> dict[str, object] | None:
    if not raw_analysis:
        return None
    try:
        analysis = json.loads(raw_analysis)
    except (TypeError, ValueError):
        return None
    return analysis if isinstance(analysis, dict) else None


def speaker_display_names(analysis: dict[str, object] | None) -> dict[str, str]:
    if not analysis or not isinstance(analysis.get("speaker_map"), list):
        return {}
    names: dict[str, str] = {}
    for item in analysis["speaker_map"]:
        if not isinstance(item, dict) or item.get("source_speaker") is None or not isinstance(item.get("display_name"), str):
            continue
        names[str(item["source_speaker"])] = item["display_name"].strip()
    return names


def record_display_title(record: object) -> str:
    analysis = parse_conversation_analysis(record_value(record, "conversation_analysis"))
    if analysis is None:
        return datetime.fromisoformat(record["recorded_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
    title = text_value(analysis.get("title"), "")
    if title:
        return title
    for topic in object_list(analysis.get("topics")):
        title = text_value(topic.get("title"), "")
        if title:
            return title
    return "空"


def render_transcript(record: object, speaker_names: dict[str, str]) -> None:
    timeline = transcript_timeline(record["transcript_timeline"])
    if not timeline:
        st.caption("逐字稿已按句子整理；该历史转写没有时间戳，无法精确跳转。")
        for sentence in split_transcript(record["transcript"]):
            st.markdown(f'<div class="transcript-line">{escape(sentence)}</div>', unsafe_allow_html=True)
        return

    st.caption("点击播放图标，即可从该句开始收听。")
    for index, sentence in enumerate(timeline):
        start_seconds = float(sentence["start_seconds"])
        controls, content = st.columns([1, 14], vertical_alignment="center")
        with controls:
            if st.button("▶", key=f"play-{record['id']}-{index}", help=f"从 {format_offset(start_seconds)} 开始播放"):
                st.session_state["pending_playback"] = {
                    "record_id": record["id"],
                    "start_seconds": start_seconds,
                }
                st.rerun()
        with content:
            speaker = sentence.get("speaker")
            label = speaker_names.get(str(speaker), f"说话人 {speaker}") if speaker is not None else ""
            speaker_label = f"<span class=\"speaker-label\">{escape(label)}</span>" if label else ""
            st.markdown(
                f'<div class="transcript-line"><span class="timestamp">{format_offset(start_seconds)}</span>'
                f"{speaker_label}{escape(str(sentence['text']))}</div>",
                unsafe_allow_html=True,
            )


def text_value(value: object, fallback: str = "记录不充分") -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def record_value(record: object, key: str, default: object = None) -> object:
    try:
        return record[key]
    except (IndexError, KeyError):
        return default


def object_list(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def render_conversation_analysis(record: object) -> None:
    analysis = parse_conversation_analysis(record_value(record, "conversation_analysis"))
    if analysis is None:
        analysis_error = record_value(record, "conversation_analysis_error")
        if analysis_error:
            st.warning(f"上次分析失败：{analysis_error}")
        if not settings.llm_key:
            st.info("未配置 DeepSeek API Key，逐字稿仍可正常查看。")
            return
        if st.button("生成对话分析", key=f"analyze-{record['id']}"):
            from worker import analyze_transcript_conversation

            try:
                with st.spinner("正在整理话题、人物和对话关系..."):
                    result = analyze_transcript_conversation(record["transcript"], record["transcript_timeline"])
                    update_segment(
                        Path(record["path"]),
                        conversation_analysis=json.dumps(result, ensure_ascii=False),
                        conversation_analysis_error=None,
                    )
                st.rerun()
            except Exception as exc:
                update_segment(Path(record["path"]), conversation_analysis_error=str(exc))
                st.error(f"分析失败：{exc}")
        return

    st.markdown("#### 摘要")
    st.write(text_value(analysis.get("overview")))

    st.markdown("#### 值得关注的事件")
    events = object_list(analysis.get("noteworthy_events"))
    if not events:
        st.caption("未发现有充分依据的值得关注事件。")
    for event in events:
        st.markdown(f"**{text_value(event.get('title'))}**  ·  {text_value(event.get('type'), '信息不足')}")
        st.write(text_value(event.get("details")))
        people = event.get("people") if isinstance(event.get("people"), list) else []
        if people:
            st.caption(f"相关人物：{'、'.join(str(person) for person in people)}")
        quote = text_value(event.get("evidence_quote"), "")
        if quote:
            st.caption(f"原话依据：{quote}")
        caution = text_value(event.get("caution"), "")
        if caution:
            st.caption(caution)

    st.markdown("#### 关键人物与关系")
    people = object_list(analysis.get("key_people"))
    if not people:
        st.caption("录音中没有足够信息确认关键人物或关系。")
    for person in people:
        st.markdown(f"**{text_value(person.get('name_or_label'))}**")
        st.write(text_value(person.get("background")))
        for relationship in object_list(person.get("relationships")):
            st.caption(
                f"与 {text_value(relationship.get('with'))}：{text_value(relationship.get('relationship'))}"
                f"。依据：{text_value(relationship.get('basis'))}"
            )

    st.markdown("#### 话题详情")
    topics = object_list(analysis.get("topics"))
    if not topics:
        st.caption("未提取到明确话题。")
    for topic in topics:
        st.markdown(f"**{text_value(topic.get('title'))}**")
        st.write(text_value(topic.get("summary")))
        participants = topic.get("participants") if isinstance(topic.get("participants"), list) else []
        if participants:
            st.caption(f"参与者：{'、'.join(str(participant) for participant in participants)}")
        for detail in object_list(topic.get("detailed_discussion")):
            st.markdown(f"{text_value(detail.get('point'))}：{text_value(detail.get('details'))}")
        st.caption(f"结论：{text_value(topic.get('outcome'), '未形成明确结论')}")
        action_items = topic.get("action_items") if isinstance(topic.get("action_items"), list) else []
        if action_items:
            st.caption(f"待办：{'；'.join(str(item) for item in action_items)}")


def period_details(choice: str, now: datetime) -> tuple[str, datetime, datetime, str]:
    midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    if choice == "今天上午":
        return "morning", midnight, min(now, midnight + timedelta(hours=12)), "今天上午"
    if choice == "本周":
        start = midnight - timedelta(days=now.weekday())
        return "week", start, now, f"本周（{start:%m-%d} 至今）"
    if choice == "本月":
        start = midnight.replace(day=1)
        return "month", start, now, f"本月（{start:%Y-%m}）"
    return "day", midnight, now, "今天"


st.set_page_config(page_title="录音档案", page_icon="R", layout="wide")
ensure_storage()
init_db()

st.markdown(
    """
    <style>
    .transcript-line {
        border-left: 3px solid #cbd5e1;
        line-height: 1.7;
        margin: 0.25rem 0;
        padding: 0.35rem 0.65rem;
    }
    .timestamp {
        color: #64748b;
        display: inline-block;
        font-family: monospace;
        margin-right: 0.65rem;
        min-width: 3.5rem;
    }
    .speaker-label {
        color: #0f766e;
        font-size: 0.85rem;
        font-weight: 600;
        margin-right: 0.5rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("录音档案")
st.caption("固定时长录音、百炼文件转写，以及按时段清晰整理")

now = datetime.now().astimezone()
choice = st.segmented_control("分析范围", ["今天", "今天上午", "本周", "本月"], default="今天")
selected_mode = st.selectbox(
    "分析模型",
    list(SUMMARY_MODE_LABELS),
    index=list(SUMMARY_MODE_LABELS).index(settings.default_summary_mode)
    if settings.default_summary_mode in SUMMARY_MODE_LABELS
    else 0,
    format_func=lambda value: SUMMARY_MODE_LABELS[value],
)
period_type, period_start, period_end, period_label = period_details(choice or "今天", now)
summary = get_period_summary(period_type, period_start)
transcript, source_count = period_transcript(period_start, period_end)

if not DB_PATH.exists():
    st.info("正在等待录音器创建数据库。")
    st.stop()

records = dashboard_segments()
statuses = sorted({record["status"] for record in records})
selected = st.multiselect("状态", statuses, default=statuses, format_func=lambda value: STATUS_LABELS.get(value, value))
filtered = [record for record in records if record["status"] in selected]

metrics = st.columns(4)
metrics[0].metric("录音片段", len(records))
metrics[1].metric("已完成", sum(record["status"] == "complete" for record in records))
metrics[2].metric("等待百炼", sum(record["status"] == "awaiting_dashscope_config" for record in records))
metrics[3].metric("已过滤静音", sum(record["status"] == "silent" for record in records))

if not filtered:
    st.info("没有符合当前筛选条件的录音。")
    st.stop()

analysis_tab, transcript_tab = st.tabs(["对话分析", "逐字稿"])

with analysis_tab:
    summary_left, summary_right = st.columns([4, 1])
    with summary_left:
        st.subheader(f"{period_label} 汇总")
        if summary:
            st.markdown(summary["summary"])
            st.caption(f"由 {summary['provider']} 基于 {summary['source_segment_count']} 段逐字稿生成")
        else:
            st.caption("该时段尚未生成汇总。")
    with summary_right:
        if st.button("生成汇总", disabled=not bool(transcript)):
            from worker import generate_period_summary

            try:
                with st.spinner("正在汇总逐字稿..."):
                    generate_period_summary(period_type, period_start, period_end, period_label, selected_mode)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
        st.caption(f"可用逐字稿：{source_count} 段")

    st.subheader("单段对话分析")
    analyzed_records = [record for record in filtered if record["transcript"]]
    if not analyzed_records:
        st.caption("当前筛选范围内还没有完成转写的录音。")
    for record in analyzed_records:
        duration = f"{record['duration_seconds'] / 60:.1f} 分钟"
        with st.expander(f"{record_display_title(record)}  |  {duration}"):
            render_conversation_analysis(record)

with transcript_tab:
    for record in filtered:
        duration = f"{record['duration_seconds'] / 60:.1f} 分钟"
        status = STATUS_LABELS.get(record["status"], record["status"])
        with st.expander(f"{record_display_title(record)}  |  {duration}  |  {status}"):
            left, right = st.columns([1, 2])
            with left:
                audio_path = Path(record["path"])
                if audio_path.exists():
                    audio_format = "audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav"
                    playback = st.session_state.get("pending_playback")
                    should_autoplay = bool(playback and playback["record_id"] == record["id"])
                    start_seconds = float(playback["start_seconds"]) if should_autoplay else 0.0
                    if should_autoplay:
                        del st.session_state["pending_playback"]
                    st.audio(str(audio_path), format=audio_format, start_time=start_seconds, autoplay=should_autoplay)
                st.caption(f"响度：{record['dbfs']:.1f} dBFS" if record["dbfs"] is not None else "等待音频分析")
                if record["active_seconds"] is not None:
                    st.caption(f"活动声音：{record['active_seconds']:.1f} 秒")
            with right:
                if record["transcript"]:
                    st.markdown("#### 逐字稿")
                    render_transcript(
                        record,
                        speaker_display_names(parse_conversation_analysis(record_value(record, "conversation_analysis"))),
                    )
                if record["error"]:
                    st.error(record["error"])
