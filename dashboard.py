from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

import streamlit as st

from config import DB_PATH, ensure_storage, settings
from storage import dashboard_segments, get_period_summary, init_db, period_transcript


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

summary_left, summary_right = st.columns([4, 1])
with summary_left:
    st.subheader(f"{period_label} 分析")
    if summary:
        st.markdown(summary["summary"])
        st.caption(f"由 {summary['provider']} 基于 {summary['source_segment_count']} 段逐字稿生成")
    else:
        st.caption("该时段尚未生成分析。")
with summary_right:
    if st.button("生成分析", disabled=not bool(transcript)):
        from worker import generate_period_summary

        try:
            with st.spinner("正在分析逐字稿..."):
                generate_period_summary(period_type, period_start, period_end, period_label, selected_mode)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    st.caption(f"可用逐字稿：{source_count} 段")

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

for record in filtered:
    label_time = datetime.fromisoformat(record["recorded_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
    duration = f"{record['duration_seconds'] / 60:.1f} 分钟"
    status = STATUS_LABELS.get(record["status"], record["status"])
    with st.expander(f"{label_time}  |  {duration}  |  {status}"):
        left, right = st.columns([1, 2])
        with left:
            audio_path = Path(record["path"])
            if audio_path.exists():
                audio_format = "audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav"
                st.audio(str(audio_path), format=audio_format)
            st.caption(f"响度：{record['dbfs']:.1f} dBFS" if record["dbfs"] is not None else "等待音频分析")
            if record["active_seconds"] is not None:
                st.caption(f"活动声音：{record['active_seconds']:.1f} 秒")
        with right:
            if record["transcript"]:
                st.markdown("#### 逐字稿")
                st.write(record["transcript"])
            if record["speech_analysis"]:
                st.markdown("#### 阿里云语音分析")
                try:
                    import json

                    st.json(json.loads(record["speech_analysis"]))
                except (TypeError, ValueError):
                    st.write(record["speech_analysis"])
            if record["error"]:
                st.error(record["error"])
