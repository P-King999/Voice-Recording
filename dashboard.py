from __future__ import annotations

import io
import json
import hmac
import os
import re
import subprocess
import sys
import time as time_module
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path

import numpy as np
import soundfile as sf
import streamlit as st
import sounddevice as sd

from config import RECORDINGS_DIR, ROOT, ensure_storage, read_env_document, save_env_document, save_env_value, settings
from exports import markdown as export_markdown, srt as export_srt, word_html
from health import _pid_alive, snapshot
from storage import (
    dashboard_counts,
    dashboard_overview,
    dashboard_segment,
    delete_segments,
    find_dashboard_overview,
    get_period_summary,
    init_db,
    list_speakers,
    period_transcript,
    summary_versions,
    update_segment,
)


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
RATING_LABELS = {
    0: "0星 无内容/未分析",
    1: "1星 教学/讲解",
    2: "2星 日常聊天",
    3: "3星 人物关系/背景",
    4: "4星 矛盾/习惯/重点内容",
    5: "5星 超级劲爆",
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


def speaker_display_names(analysis: dict[str, object] | None, aliases: dict[str, str]) -> dict[str, str]:
    names = dict(aliases)
    if not analysis or not isinstance(analysis.get("speaker_map"), list):
        return names
    for item in analysis["speaker_map"]:
        if not isinstance(item, dict) or item.get("source_speaker") is None or not isinstance(item.get("display_name"), str):
            continue
        names.setdefault(str(item["source_speaker"]), item["display_name"].strip())
    return names


def record_display_title(record: object) -> str:
    recorded_at = record_recorded_at(record)
    timestamp = recorded_at.strftime("%Y-%m-%d %H:%M")
    analysis = parse_conversation_analysis(record_value(record, "conversation_analysis"))
    stars = "★" * excitement_rating(record, analysis)
    if analysis is None:
        return f"{stars}  |  {timestamp}"
    title = text_value(analysis.get("title"), "")
    if title:
        return f"{stars}  {title}  |  {timestamp}"
    for topic in object_list(analysis.get("topics")):
        title = text_value(topic.get("title"), "")
        if title:
            return f"{stars}  {title}  |  {timestamp}"
    return f"{stars}  空  |  {timestamp}"


def record_recorded_at(record: object) -> datetime:
    return datetime.fromisoformat(record["recorded_at"]).astimezone()


def excitement_rating(record: object, analysis: dict[str, object] | None = None) -> int:
    if analysis is None and not record_value(record, "conversation_analysis"):
        return 0
    value = analysis.get("excitement_rating") if analysis else record_value(record, "excitement_rating", 1)
    try:
        return min(5, max(1, int(value)))
    except (TypeError, ValueError):
        return 1


def highlight_fragments(analysis: dict[str, object] | None) -> list[str]:
    if not analysis or excitement_rating({}, analysis) < 3:
        return []
    quotes = analysis.get("highlight_quotes")
    fragments = [item.strip() for item in quotes if isinstance(item, str) and item.strip()] if isinstance(quotes, list) else []
    for event in object_list(analysis.get("noteworthy_events")):
        try:
            event_rating = int(event.get("excitement_rating", analysis.get("excitement_rating", 1)))
        except (TypeError, ValueError):
            event_rating = 1
        quote = event.get("evidence_quote")
        if event_rating >= 3 and isinstance(quote, str) and quote.strip():
            fragments.append(quote.strip())
    return list(dict.fromkeys(fragments))


def transcript_html(text: str, fragments: list[str]) -> str:
    escaped = escape(text)
    return f"<strong>{escaped}</strong>" if any(fragment in text for fragment in fragments) else escaped


def active_recordings() -> list[tuple[Path, float]]:
    bytes_per_second = settings.sample_rate * settings.channels * 2
    cutoff = datetime.now().timestamp() - 120
    return [
        (path, max(0, (path.stat().st_size - 44) / bytes_per_second))
        for path in sorted(RECORDINGS_DIR.glob("*.part.wav"), key=lambda item: item.stat().st_mtime, reverse=True)
        if path.exists() and path.stat().st_mtime >= cutoff
    ]


def live_audio_snapshot() -> tuple[Path, np.ndarray, float] | None:
    """Read a short tail from the file the recorder is currently writing."""
    active = active_recordings()
    if not active:
        return None
    path, _ = active[0]
    try:
        with sf.SoundFile(path) as audio:
            frame_count = min(len(audio), int(audio.samplerate * 1.5))
            if not frame_count:
                return None
            audio.seek(-frame_count, whence=2)
            samples = audio.read(frame_count, dtype="float32", always_2d=True)
    except (OSError, RuntimeError):
        return None
    mono = samples.mean(axis=1)
    rms = float(np.sqrt(np.mean(np.square(mono))))
    dbfs = 20 * float(np.log10(max(rms, 1e-12)))
    return path, mono, dbfs


@st.cache_data(ttl=8, show_spinner=False)
def load_dashboard_records() -> list[dict[str, object]]:
    return [dict(record) for record in dashboard_overview(limit=120)]


@st.cache_data(ttl=8, show_spinner=False)
def load_record(record_id: int) -> dict[str, object] | None:
    record = dashboard_segment(record_id)
    return dict(record) if record else None


@st.cache_data(ttl=8, show_spinner=False)
def search_records(
    query: str,
    statuses: tuple[str, ...],
    recorded_on: object | None,
    ratings: tuple[int, ...],
    favorites_only: bool,
    newest_first: bool = True,
    page: int = 0,
) -> tuple[list[dict[str, object]], int]:
    records, total = find_dashboard_overview(
        query, list(statuses), recorded_on, list(ratings), favorites_only,
        newest_first, limit=100, offset=max(0, page) * 100,
    )
    return [dict(record) for record in records], total


def archive_groups(records: list[object], now: datetime) -> list[tuple[str, str, list[object]]]:
    """Arrange newest records into the day, week, and month archive hierarchy."""
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    buckets: dict[tuple[str, str], list[object]] = {}

    for record in records:
        recorded_at = record_recorded_at(record)
        recorded_date = recorded_at.date()
        if recorded_date == today:
            key = ("本日", "上午" if recorded_at.hour < 12 else "下午")
        elif recorded_date >= week_start:
            key = ("本周", recorded_at.strftime("%m月%d日"))
        elif recorded_date >= month_start:
            key = ("本月", recorded_at.strftime("%m月%d日"))
        else:
            key = (recorded_at.strftime("%Y年%m月"), recorded_at.strftime("%m月%d日"))
        buckets.setdefault(key, []).append(record)

    return [(heading, subheading, items) for (heading, subheading), items in buckets.items()]


def render_archive_groups(records: list[object], now: datetime, render_record: object) -> None:
    previous_heading = ""
    for heading, subheading, items in archive_groups(records, now):
        if heading != previous_heading:
            st.markdown(f"#### {heading}")
            previous_heading = heading
        st.caption(subheading)
        for record in items:
            render_record(record)


def render_transcript(record: object, speaker_names: dict[str, str], widget_prefix: str = "") -> None:
    timeline = transcript_timeline(record["transcript_timeline"])
    fragments = highlight_fragments(parse_conversation_analysis(record_value(record, "conversation_analysis")))
    if not timeline:
        st.caption("逐字稿已按句子整理；该历史转写没有时间戳，无法精确跳转。")
        for sentence in split_transcript(record["transcript"]):
            st.markdown(f'<div class="transcript-line">{transcript_html(sentence, fragments)}</div>', unsafe_allow_html=True)
        return

    st.caption("点击播放图标，即可从该句开始收听。")
    for index, sentence in enumerate(timeline):
        start_seconds = float(sentence["start_seconds"])
        controls, content = st.columns([1, 14], vertical_alignment="center")
        with controls:
            if st.button("▶", key=f"{widget_prefix}-play-{record['id']}-{index}", help=f"从 {format_offset(start_seconds)} 开始播放"):
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
                f"{speaker_label}{transcript_html(str(sentence['text']), fragments)}</div>",
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


def render_conversation_analysis(record: object, widget_prefix: str = "") -> None:
    analysis = parse_conversation_analysis(record_value(record, "conversation_analysis"))
    if analysis is None:
        analysis_error = record_value(record, "conversation_analysis_error")
        if analysis_error:
            st.warning(f"上次分析失败：{analysis_error}")
        transcript = record_value(record, "transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            st.info("该录音还没有可用的逐字稿，完成转写后才能生成对话分析。")
            return
        if not settings.llm_key:
            st.info("未配置 DeepSeek API Key，逐字稿仍可正常查看。")
            return
        if st.button("生成对话分析", key=f"{widget_prefix}-analyze-{record['id']}"):
            from worker import analyze_transcript_conversation

            try:
                with st.spinner("正在整理话题、人物和对话关系..."):
                    result = analyze_transcript_conversation(record["transcript"], record["transcript_timeline"])
                    update_segment(
                        int(record["id"]),
                        conversation_analysis=json.dumps(result, ensure_ascii=False),
                        conversation_analysis_error=None,
                        excitement_rating=excitement_rating(record, result),
                    )
                load_dashboard_records.clear()
                st.rerun()
            except Exception as exc:
                update_segment(int(record["id"]), conversation_analysis_error=str(exc))
                st.error(f"分析失败：{exc}")
        return

    rating = excitement_rating(record, analysis)
    st.markdown(f"#### {'★' * rating} {RATING_LABELS[rating]}")
    if st.button("重新分析并评分", key=f"{widget_prefix}-rerate-{record['id']}"):
        from worker import analyze_transcript_conversation

        try:
            with st.spinner("正在按星级规则重新整理... "):
                result = analyze_transcript_conversation(record["transcript"], record["transcript_timeline"])
                update_segment(
                    int(record["id"]),
                    conversation_analysis=json.dumps(result, ensure_ascii=False),
                    conversation_analysis_error=None,
                    excitement_rating=excitement_rating(record, result),
                )
            load_dashboard_records.clear()
            st.rerun()
        except Exception as exc:
            update_segment(int(record["id"]), conversation_analysis_error=str(exc))
            st.error(f"重新分析失败：{exc}")

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
            rendered_items = []
            for item in action_items:
                if isinstance(item, dict):
                    rendered_items.append(
                        f"{text_value(item.get('task'))}（负责人：{text_value(item.get('owner'), '未明确')}；截止：{text_value(item.get('due_date'), '未明确')}）"
                    )
                else:
                    rendered_items.append(str(item))
            st.caption(f"待办：{'；'.join(rendered_items)}")


def period_details(choice: str, now: datetime) -> tuple[str, datetime, datetime, str]:
    midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    noon = midnight + timedelta(hours=12)
    if choice == "本日上午":
        return "morning", midnight, min(now, noon), "本日上午"
    if choice == "本日下午":
        return "afternoon", noon, max(now, noon), "本日下午"
    if choice == "本日晚上":
        start = midnight + timedelta(hours=17)
        return "evening", start, max(now, start), "本日晚上"
    if choice == "本周":
        start = midnight - timedelta(days=now.weekday())
        return "week", start, now, f"本周（{start:%m-%d} 至今）"
    if choice == "本月":
        start = midnight.replace(day=1)
        return "month", start, now, f"本月（{start:%Y-%m}）"
    return "day", midnight, now, "本日"


def require_authentication() -> None:
    if not settings.dashboard_password:
        st.error("看板尚未配置 DASHBOARD_PASSWORD，已拒绝访问。")
        st.stop()
    if st.session_state.get("authenticated"):
        return
    st.subheader("录音档案登录")
    with st.form("dashboard-login"):
        password = st.text_input("密码", type="password")
        submitted = st.form_submit_button("登录")
    if submitted and hmac.compare_digest(password, settings.dashboard_password):
        st.session_state["authenticated"] = True
        st.rerun()
    if submitted:
        st.error("密码错误。")
    st.stop()


st.set_page_config(page_title="录音档案", page_icon="R", layout="wide")
require_authentication()
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

def device_family(name: str) -> str:
    """Group Windows API copies of the same physical microphone."""
    normalized = re.sub(r"\s+", " ", name.lower()).strip()
    if "realtek" in normalized:
        return "realtek"
    if "mic device" in normalized:
        return "mic-device"
    if "edifier" in normalized:
        return "edifier"
    normalized = re.sub(r"\([^)]*\)", "", normalized)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalized)


def input_device_options() -> tuple[list[str], dict[str, str]]:
    """Show physical microphones once, hiding Windows aliases and virtual endpoints."""
    options = ["auto"]
    labels = {"auto": "自动检测（选择声音最大的麦克风）"}
    hidden_markers = (
        "sound mapper", "主声音捕获驱动程序", "primary sound capture", "stereo mix", "立体声混音",
        "steelseries sonar", "vb-audio", "virtual cable", "voicemeeter",
    )
    try:
        devices = sd.query_devices()
    except Exception:
        return options, labels

    candidates: dict[str, tuple[int, str]] = {}
    for index, device in enumerate(devices):
        if int(device["max_input_channels"]) <= 0:
            continue
        name = str(device["name"])
        if any(marker in name.lower() for marker in hidden_markers):
            continue
        family = device_family(name)
        if not family:
            continue
        api_name = str(sd.query_hostapis(device["hostapi"])["name"])
        # Prefer WASAPI, then DirectSound, over legacy MME and WDM-KS endpoints.
        priority = {"Windows WASAPI": 0, "Windows DirectSound": 1, "MME": 2, "Windows WDM-KS": 3}.get(api_name, 4)
        if family not in candidates or priority < candidates[family][0]:
            candidates[family] = (priority, name)
    for _, name in candidates.values():
        options.append(name)
        labels[name] = name
    return options, labels


def saved_recording_device() -> str:
    for line in read_env_document().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "RECORDING_DEVICE":
            return value.strip() or "auto"
    return "auto"


def microphone_test(device_choice: str) -> tuple[bytes, float]:
    """Record three seconds from the selected device and return a playable WAV clip."""
    if device_choice == "auto":
        # Keep the test aligned with the recorder's automatic selection rule.
        from recorder import select_loudest_input_device

        device: str | int | None = select_loudest_input_device()
    else:
        device = device_choice
    samples = sd.rec(
        int(settings.sample_rate * 3),
        samplerate=settings.sample_rate,
        channels=settings.channels,
        dtype="float32",
        device=device,
    )
    sd.wait()
    rms = float(np.sqrt(np.mean(np.square(samples))))
    dbfs = 20 * float(np.log10(max(rms, 1e-12)))
    wav = io.BytesIO()
    sf.write(wav, samples, settings.sample_rate, format="WAV", subtype="PCM_16")
    return wav.getvalue(), dbfs


def restart_recorder(device_choice: str) -> None:
    """Ask the recorder to finish its current audio block, then start it again."""
    runtime_dir = ROOT / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pid_path = runtime_dir / "recorder.pid"
    restart_path = runtime_dir / "recorder.restart"

    if _pid_alive(pid_path):
        restart_path.touch()
        deadline = time_module.monotonic() + 15
        while _pid_alive(pid_path) and time_module.monotonic() < deadline:
            time_module.sleep(0.1)
        if _pid_alive(pid_path):
            raise RuntimeError("录音器未能在 15 秒内停止，请稍后重试。")

    pid_path.unlink(missing_ok=True)
    restart_path.unlink(missing_ok=True)
    environment = os.environ.copy()
    environment["RECORDING_DEVICE"] = device_choice
    process = subprocess.Popen(
        [sys.executable, "recorder.py"],
        cwd=ROOT,
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    pid_path.write_text(str(process.pid), encoding="utf-8")
    time_module.sleep(1)
    if process.poll() is not None:
        raise RuntimeError("录音器启动失败，请查看 runtime/recorder.error.log。")


def render_live_recording() -> None:
    active = active_recordings()
    if active:
        path, seconds = active[0]
        st.success(f"正在录音  |  {path.stem.replace('.part', '')}  |  已录 {seconds / 60:.1f} 分钟")
    elif health["services"].get("recorder"):
        st.info("录音器正在运行，等待第一个音频块写入。")
    else:
        st.warning("录音器未运行。")


def render_live_monitor() -> None:
    snapshot = live_audio_snapshot()
    if snapshot is None:
        st.warning("等待音频输入")
        return
    path, samples, dbfs = snapshot
    signal_present = dbfs >= settings.silence_threshold_dbfs
    status = "正在录入声音" if signal_present else "当前环境安静"
    meter_value = int(min(100, max(0, (dbfs + 80) / 80 * 100)))
    monitor_cols = st.columns([1, 2])
    monitor_cols[0].metric("实时音量", f"{dbfs:.1f} dBFS", status)
    monitor_cols[0].progress(meter_value)
    # Limit points so the chart remains lightweight during one-second refreshes.
    stride = max(1, len(samples) // 300)
    monitor_cols[1].line_chart(samples[::stride], height=120, use_container_width=True)
    st.caption(f"监听中  |  {path.stem.replace('.part', '')}")


def clear_record_caches() -> None:
    load_dashboard_records.clear()
    load_record.clear()
    search_records.clear()


def render_record_detail(record: dict[str, object], widget_prefix: str) -> None:
    try:
        current_tags = ", ".join(json.loads(str(record["tags"] or "[]")))
    except (TypeError, ValueError):
        current_tags = ""
    actions = st.columns([1, 3, 5, 1])
    detail_prefix = f"detail-{widget_prefix}"
    if actions[0].button("取消收藏" if record["favorite"] else "收藏", key=f"{detail_prefix}-favorite-{record['id']}"):
        toggle_favorite(int(record["id"]), record["favorite"])
        st.rerun()
    tags = actions[1].text_input("标签", value=current_tags, key=f"{detail_prefix}-tags-{record['id']}")
    notes = actions[2].text_input("备注", value=str(record["notes"] or ""), key=f"{detail_prefix}-notes-{record['id']}")
    if actions[3].button("保存备注", key=f"{detail_prefix}-save-{record['id']}"):
        update_segment(int(record["id"]), tags=json.dumps([item.strip() for item in tags.split(",") if item.strip()], ensure_ascii=False), notes=notes)
        clear_record_caches()
        st.rerun()
    analysis_tab, transcript_tab = st.tabs(["对话分析", "逐字稿与音频"])
    with analysis_tab:
        render_conversation_analysis(record, widget_prefix)
    with transcript_tab:
        left, right = st.columns([1, 2])
        with left:
            audio_path = Path(str(record["path"]))
            if audio_path.exists():
                st.audio(str(audio_path), format="audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav")
            st.caption(f"响度：{record['dbfs']:.1f} dBFS" if record["dbfs"] is not None else "等待音频分析")
        with right:
            if record["transcript"]:
                render_transcript(record, speaker_display_names(parse_conversation_analysis(record_value(record, "conversation_analysis")), list_speakers()), widget_prefix)
            elif record["error"]:
                st.error(str(record["error"]))
            else:
                st.info("该片段尚未完成转写。")


def overview_title(record: dict[str, object]) -> str:
    """Prefer the analysis title without loading full analysis JSON for the home list."""
    title = str(record.get("display_title") or "").strip()
    if not title:
        analysis = parse_conversation_analysis(str(record.get("conversation_analysis") or ""))
        title = text_value(analysis.get("title"), "") if analysis else ""
    return title if title else "空"


def is_empty_record(record: dict[str, object]) -> bool:
    """Keep recordings without a meaningful analysis title out of the default list."""
    return overview_title(record) == "空"


def display_rating(record: dict[str, object]) -> int:
    value = record.get("display_rating", excitement_rating(record))
    try:
        return min(5, max(0, int(value)))
    except (TypeError, ValueError):
        return 0


def rating_label(record: dict[str, object]) -> str:
    rating = display_rating(record)
    return "0星" if rating == 0 else "★" * rating


def toggle_favorite(record_id: int, favorite: object) -> None:
    update_segment(record_id, favorite=0 if favorite else 1)
    clear_record_caches()


st.title("录音档案")
home_tab, archive_tab, settings_tab = st.tabs(["首页", "搜索与档案", "设置"])

with home_tab:
    health = snapshot(ROOT)
    health_cols = st.columns(4)
    health_cols[0].metric("磁盘可用", f"{health['free_gb']:.1f} GB")
    health_cols[1].metric("数据库", "正常" if health["database_ok"] else "异常")
    health_cols[2].metric("录音器", "运行中" if health["services"].get("recorder") else "未运行")
    health_cols[3].metric("处理器", "运行中" if health["services"].get("worker") else "未运行")
    if health["free_gb"] < settings.disk_warning_gb:
        st.error(f"磁盘空间不足：仅剩 {health['free_gb']:.1f} GB，请及时归档或清理录音。")
    if hasattr(st, "fragment"):
        st.fragment(run_every=10)(render_live_recording)()
    else:
        render_live_recording()
    counts = dashboard_counts()
    metrics = st.columns(4)
    metrics[0].metric("全部片段", counts.get("all", 0))
    metrics[1].metric("已完成", counts.get("complete", 0))
    metrics[2].metric("等待处理", counts.get("queued", 0) + counts.get("awaiting_dashscope_config", 0))
    metrics[3].metric("处理失败", counts.get("failed", 0))
    st.subheader("时段汇总")
    now = datetime.now().astimezone()
    choice = st.segmented_control("汇总范围", ["本日", "本日上午", "本日下午", "本日晚上", "本周", "本月"], default="本日", key="home-summary-period")
    period_type, period_start, period_end, period_label = period_details(choice or "本日", now)
    summary = get_period_summary(period_type, period_start)
    summary_left, summary_right = st.columns([5, 1])
    with summary_left:
        if summary:
            st.markdown(summary["summary"])
            st.caption(f"{period_label} · {summary['provider']} · 基于 {summary['source_segment_count']} 段逐字稿")
        else:
            st.caption(f"{period_label} 尚未生成汇总。")
    with summary_right:
        selected_mode = st.selectbox("模型", list(SUMMARY_MODE_LABELS), format_func=lambda value: SUMMARY_MODE_LABELS[value], key="home-summary-model")
        if st.button("生成汇总", key="home-generate-summary"):
            with st.spinner("正在读取该时段逐字稿并生成汇总..."):
                transcript, source_count = period_transcript(period_start, period_end)
                if not transcript:
                    st.warning("该时段没有可用逐字稿。")
                else:
                    from worker import generate_period_summary
                    generate_period_summary(period_type, period_start, period_end, period_label, selected_mode)
                    st.success(f"已基于 {source_count} 段逐字稿生成汇总。")
                    st.rerun()
    st.subheader("本日和最近录音")
    recent = load_dashboard_records()
    if recent:
        today_records = [item for item in recent if record_recorded_at(item).date() == now.date()]
        today_column, recent_column = st.columns(2)
        for heading, items, column, prefix in (
            ("本日", today_records, today_column, "today"),
            ("最近录音", recent, recent_column, "recent"),
        ):
            with column:
                st.markdown(f"#### {heading}")
                if not items:
                    st.caption("暂无录音")
                visible_items = [item for item in items if not is_empty_record(item)][:8]
                empty_items = [item for item in items if is_empty_record(item)]
                for item in visible_items:
                    status = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
                    title, favorite_action, button = st.columns([4, 1, 1])
                    title.write(overview_title(item))
                    title.caption(f"{rating_label(item)} · {record_recorded_at(item):%m-%d %H:%M} · {item['duration_seconds'] / 60:.1f} 分钟 · {status}")
                    if favorite_action.button("★" if item["favorite"] else "☆", key=f"{prefix}-favorite-{item['id']}", help="取消收藏" if item["favorite"] else "收藏"):
                        toggle_favorite(int(item["id"]), item["favorite"])
                        st.rerun()
                    if button.button("查看", key=f"{prefix}-{item['id']}"):
                        st.session_state["home_record_id"] = int(item["id"])
                        st.rerun()
                if empty_items:
                    with st.expander(f"空（{len(empty_items)} 条）", expanded=False):
                        for item in empty_items:
                            status = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
                            title, favorite_action, button = st.columns([4, 1, 1])
                            title.write(overview_title(item))
                            title.caption(f"{rating_label(item)} · {record_recorded_at(item):%m-%d %H:%M} · {item['duration_seconds'] / 60:.1f} 分钟 · {status}")
                            if favorite_action.button("★" if item["favorite"] else "☆", key=f"{prefix}-empty-favorite-{item['id']}", help="取消收藏" if item["favorite"] else "收藏"):
                                toggle_favorite(int(item["id"]), item["favorite"])
                                st.rerun()
                            if button.button("查看", key=f"{prefix}-empty-{item['id']}"):
                                st.session_state["home_record_id"] = int(item["id"])
                                st.rerun()
        if "home_record_id" not in st.session_state:
            st.session_state["home_record_id"] = int((today_records or recent)[0]["id"])
        home_record_id = st.session_state.get("home_record_id")
        if home_record_id:
            selected_record = load_record(int(home_record_id))
            if selected_record:
                st.divider()
                st.subheader(overview_title(selected_record))
                render_record_detail(selected_record, "home")
    else:
        st.info("还没有录音片段。")

with archive_tab:
    archive_title, favorites_action = st.columns([6, 1])
    archive_title.subheader("全部录音")
    if favorites_action.button("收藏夹", key="archive-favorites-toggle", type="secondary"):
        st.session_state["archive_favorites_only"] = not bool(st.session_state.get("archive_favorites_only", False))
        st.session_state["archive_page"] = 0
        st.session_state.pop("archive_record_id", None)
        st.rerun()
    if st.session_state.get("archive_favorites_only", False):
        st.caption("正在显示收藏夹")
    with st.form("archive-search", border=False):
        filter_cols = st.columns([3, 2, 2, 2, 2, 1])
        query = filter_cols[0].text_input("关键词", placeholder="逐字稿、人物、话题、标签或备注")
        date_enabled = filter_cols[1].checkbox("限定日期")
        filter_date = filter_cols[1].date_input("录音日期", value=datetime.now().date(), disabled=not date_enabled)
        selected_statuses = filter_cols[2].multiselect("状态", list(STATUS_LABELS), default=list(STATUS_LABELS), format_func=lambda value: STATUS_LABELS[value])
        selected_ratings = filter_cols[3].multiselect("星级", list(range(6)), default=list(range(6)), format_func=lambda value: RATING_LABELS[value])
        sort_order = filter_cols[4].selectbox("排序", ["最近优先", "最早优先"])
        submitted = filter_cols[5].form_submit_button("应用", use_container_width=True)
    if submitted or "archive_query" not in st.session_state:
        st.session_state["archive_query"] = query
        st.session_state["archive_statuses"] = tuple(selected_statuses)
        st.session_state["archive_date"] = filter_date if date_enabled else None
        st.session_state["archive_ratings"] = tuple(selected_ratings)
        st.session_state["archive_newest_first"] = sort_order == "最近优先"
        st.session_state["archive_page"] = 0
        st.session_state.pop("archive_record_id", None)
    page = int(st.session_state.get("archive_page", 0))
    results, total_results = search_records(
        st.session_state["archive_query"],
        st.session_state["archive_statuses"],
        st.session_state["archive_date"],
        st.session_state["archive_ratings"],
        bool(st.session_state.get("archive_favorites_only", False)),
        bool(st.session_state["archive_newest_first"]),
        page,
    )
    page_size = 100
    page_count = max(1, (total_results + page_size - 1) // page_size)
    st.caption(f"共 {total_results} 条录音，当前显示第 {page + 1} / {page_count} 页。")
    if results:
        visible_results = [item for item in results if not is_empty_record(item)]
        empty_results = [item for item in results if is_empty_record(item)]
        for item in visible_results:
            status = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
            title, timestamp, favorite_action, action = st.columns([5, 3, 1, 1])
            title.write(overview_title(item))
            timestamp.caption(f"{rating_label(item)} · {record_recorded_at(item):%Y-%m-%d %H:%M} · {item['duration_seconds'] / 60:.1f} 分钟 · {status}")
            if favorite_action.button("★" if item["favorite"] else "☆", key=f"archive-favorite-{item['id']}", help="取消收藏" if item["favorite"] else "收藏"):
                toggle_favorite(int(item["id"]), item["favorite"])
                st.rerun()
            if action.button("查看", key=f"archive-view-{item['id']}"):
                st.session_state["archive_record_id"] = int(item["id"])
                st.rerun()
        if empty_results:
            with st.expander(f"空（{len(empty_results)} 条）", expanded=False):
                for item in empty_results:
                    status = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
                    title, timestamp, favorite_action, action = st.columns([5, 3, 1, 1])
                    title.write(overview_title(item))
                    timestamp.caption(f"{rating_label(item)} · {record_recorded_at(item):%Y-%m-%d %H:%M} · {item['duration_seconds'] / 60:.1f} 分钟 · {status}")
                    if favorite_action.button("★" if item["favorite"] else "☆", key=f"archive-empty-favorite-{item['id']}", help="取消收藏" if item["favorite"] else "收藏"):
                        toggle_favorite(int(item["id"]), item["favorite"])
                        st.rerun()
                    if action.button("查看", key=f"archive-empty-view-{item['id']}"):
                        st.session_state["archive_record_id"] = int(item["id"])
                        st.rerun()
        pager_left, pager_center, pager_right = st.columns([1, 4, 1])
        if pager_left.button("上一页", disabled=page == 0):
            st.session_state["archive_page"] = page - 1
            st.rerun()
        pager_center.caption(f"第 {page + 1} 页，共 {page_count} 页")
        if pager_right.button("下一页", disabled=page + 1 >= page_count):
            st.session_state["archive_page"] = page + 1
            st.rerun()
    else:
        st.info("没有符合条件的录音。")
    archive_record_id = st.session_state.get("archive_record_id")
    if archive_record_id:
        selected_record = load_record(int(archive_record_id))
        if selected_record:
            st.divider()
            render_record_detail(selected_record, "archive")

    with st.expander("批量操作", expanded=False):
        selected_ids = st.multiselect(
            "选择当前结果中的录音",
            [int(item["id"]) for item in results],
            format_func=lambda item: overview_title(next(candidate for candidate in results if int(candidate["id"]) == item)),
        )
        selected_records = [load_record(item) for item in selected_ids]
        selected_records = [item for item in selected_records if item]
        actions = st.columns(4)
        if actions[0].button("批量重试", disabled=not selected_records):
            for item in selected_records:
                update_segment(int(item["id"]), status="queued", attempts=0, error=None, lease_owner=None, lease_expires_at=None)
            clear_record_caches()
            st.rerun()
        if actions[1].button("删除选中项", disabled=not selected_records):
            st.session_state["confirm_delete"] = True
        if st.session_state.get("confirm_delete") and selected_records:
            if st.checkbox("确认永久删除选中的录音和记录"):
                paths = delete_segments([int(item["id"]) for item in selected_records])
                for path in paths:
                    Path(path).unlink(missing_ok=True)
                st.session_state["confirm_delete"] = False
                clear_record_caches()
                st.rerun()
        actions[2].download_button("导出 Markdown", export_markdown(selected_records), "recordings.md", "text/markdown", disabled=not selected_records)
        actions[3].download_button("导出 Word", word_html(selected_records), "recordings.doc", "application/msword", disabled=not selected_records)
        st.download_button("导出 SRT", export_srt(selected_records), "recordings.srt", "application/x-subrip", disabled=not selected_records)

with settings_tab:
    st.subheader("录音设备与监听")
    device_options, device_labels = input_device_options()
    selected_device = saved_recording_device()
    if selected_device not in device_options:
        device_options.append(selected_device)
        device_labels[selected_device] = f"已保存设备：{selected_device}（当前不可用）"
    device_choice = st.selectbox(
        "麦克风",
        device_options,
        index=device_options.index(selected_device),
        format_func=lambda item: device_labels[item],
    )
    device_actions = st.columns(2)
    if device_actions[0].button("测试麦克风"):
        try:
            with st.spinner("正在录制 3 秒测试音频..."):
                clip, dbfs = microphone_test(device_choice)
        except Exception as exc:
            st.error(f"无法使用该麦克风：{exc}")
        else:
            st.session_state["microphone_test_clip"] = clip
            st.session_state["microphone_test_dbfs"] = dbfs
    if "microphone_test_clip" in st.session_state:
        test_dbfs = float(st.session_state["microphone_test_dbfs"])
        st.audio(st.session_state["microphone_test_clip"], format="audio/wav")
        st.success(f"声音测试完成：{test_dbfs:.1f} dBFS" if test_dbfs >= settings.silence_threshold_dbfs else f"声音测试完成，但音量偏低：{test_dbfs:.1f} dBFS")
    if device_actions[1].button("保存并切换"):
        try:
            if device_choice != "auto":
                sd.check_input_settings(
                    device=device_choice,
                    samplerate=settings.sample_rate,
                    channels=settings.channels,
                    dtype="float32",
                )
            save_env_value("RECORDING_DEVICE", device_choice)
            restart_recorder(device_choice)
        except Exception as exc:
            st.error(f"切换失败：{exc}")
        else:
            st.session_state["recording_device_changed"] = device_labels[device_choice]
            st.rerun()
    if changed_device := st.session_state.pop("recording_device_changed", None):
        st.success(f"已切换到 {changed_device}，录音器已重新启动。")
    if st.toggle("实时监听", key="live_monitor_enabled"):
        if hasattr(st, "fragment"):
            st.fragment(run_every=1)(render_live_monitor)()
        else:
            render_live_monitor()

    st.divider()
    st.subheader("密钥与运行配置")
    st.caption("配置保存在项目根目录的 .env 文档中；密钥默认隐藏。保存后，录音设备立即重启生效，其余后台参数在下次重启服务后生效。")
    reveal_secrets = st.checkbox("显示 API Key 和密码", value=False)
    def env_value(key: str) -> str:
        return os.getenv(key, "")
    if st.button("将当前已加载的密钥迁移到 .env"):
        save_env_value("DASHSCOPE_API_KEY", env_value("DASHSCOPE_API_KEY"))
        save_env_value("LLM_API_KEY", env_value("LLM_API_KEY"))
        st.success("当前已加载的 API Key 已写入 .env。确认可用后，可自行删除旧的密钥文件。")
    with st.form("runtime-settings"):
        api_left, api_right = st.columns(2)
        dashscope_key = api_left.text_input("百炼 API Key", value=env_value("DASHSCOPE_API_KEY"), type="default" if reveal_secrets else "password")
        llm_key = api_right.text_input("DeepSeek API Key", value=env_value("LLM_API_KEY"), type="default" if reveal_secrets else "password")
        recording_cols = st.columns(3)
        segment_seconds = recording_cols[0].number_input("每段录音秒数", min_value=60, value=int(env_value("SEGMENT_SECONDS") or settings.segment_seconds), step=60)
        silence_threshold = recording_cols[1].number_input("静音阈值 dBFS", value=float(env_value("SILENCE_THRESHOLD_DBFS") or settings.silence_threshold_dbfs), step=1.0)
        active_seconds = recording_cols[2].number_input("最短有效声音秒数", min_value=0.0, value=float(env_value("MIN_ACTIVE_SECONDS") or settings.min_active_seconds), step=0.5)
        model_cols = st.columns(2)
        summary_mode = model_cols[0].selectbox("默认汇总模型", list(SUMMARY_MODE_LABELS), index=list(SUMMARY_MODE_LABELS).index(env_value("DEFAULT_SUMMARY_MODE") or settings.default_summary_mode), format_func=lambda value: SUMMARY_MODE_LABELS[value])
        dashboard_password = model_cols[1].text_input("看板访问密码", value=env_value("DASHBOARD_PASSWORD"), type="default" if reveal_secrets else "password")
        warning_gb = st.number_input("磁盘警告阈值（GB）", min_value=1.0, value=float(env_value("DISK_WARNING_GB") or settings.disk_warning_gb), step=1.0)
        st.divider()
        st.subheader("邮件日报与查询")
        st.caption("QQ 邮箱的 SMTP/IMAP 服务均需开启，并使用授权码，不要填写 QQ 登录密码。保存后请重启后台服务。")
        report_enabled = st.checkbox("启用定时邮件日报", value=(env_value("REPORT_ENABLED") or str(settings.report_enabled)).lower() == "true")
        report_cols = st.columns(2)
        report_times = report_cols[0].text_input("日报发送时间（24 小时制，逗号分隔）", value=env_value("REPORT_TIMES") or ",".join(settings.report_times), placeholder="12:10,18:10")
        report_recipient = report_cols[1].text_input("接收邮箱", value=env_value("REPORT_EMAIL_RECIPIENT") or settings.report_email_recipient)
        smtp_cols = st.columns(3)
        smtp_username = smtp_cols[0].text_input("QQ 邮箱账号", value=env_value("REPORT_SMTP_USERNAME") or settings.report_smtp_username, placeholder="name@qq.com")
        smtp_host = smtp_cols[1].text_input("SMTP 服务器", value=env_value("REPORT_SMTP_HOST") or settings.report_smtp_host)
        smtp_port = smtp_cols[2].number_input("SMTP 端口", min_value=1, max_value=65535, value=int(env_value("REPORT_SMTP_PORT") or settings.report_smtp_port), step=1)
        smtp_auth_code = st.text_input("QQ 邮箱 SMTP 授权码", value=env_value("REPORT_SMTP_AUTH_CODE"), type="password", help="在 QQ 邮箱设置中开启 SMTP 后生成；不会以明文显示。")
        st.markdown("**邮件查询方式**：从上述 QQ 邮箱给自己发送任意邮件，系统会回复当前报告；在主题或正文写 `2026-08-12` 或 `2026年8月12日`，会回复对应日期的历史报告。系统只处理该邮箱自己发给自己的邮件，自动日报不会触发回复。")
        query_cols = st.columns(3)
        report_query_enabled = query_cols[0].checkbox("启用收件查询", value=(env_value("REPORT_QUERY_ENABLED") or str(settings.report_query_enabled)).lower() == "true")
        imap_host = query_cols[1].text_input("IMAP 服务器", value=env_value("REPORT_IMAP_HOST") or settings.report_imap_host)
        imap_port = query_cols[2].number_input("IMAP 端口", min_value=1, max_value=65535, value=int(env_value("REPORT_IMAP_PORT") or settings.report_imap_port), step=1)
        query_interval = st.number_input("收件查询间隔（秒）", min_value=20, value=int(env_value("REPORT_QUERY_INTERVAL_SECONDS") or settings.report_query_interval_seconds), step=10)
        save_settings = st.form_submit_button("保存全部设置")
    if save_settings:
        values = {
            "DASHSCOPE_API_KEY": dashscope_key.strip(), "LLM_API_KEY": llm_key.strip(),
            "SEGMENT_SECONDS": str(int(segment_seconds)), "SILENCE_THRESHOLD_DBFS": str(silence_threshold),
            "MIN_ACTIVE_SECONDS": str(active_seconds), "DEFAULT_SUMMARY_MODE": summary_mode,
            "DASHBOARD_PASSWORD": dashboard_password, "DISK_WARNING_GB": str(warning_gb),
            "REPORT_ENABLED": str(report_enabled).lower(), "REPORT_TIMES": report_times.strip(),
            "REPORT_CHANNELS": "smtp", "REPORT_EMAIL_RECIPIENT": report_recipient.strip(),
            "REPORT_SMTP_USERNAME": smtp_username.strip(), "REPORT_SMTP_HOST": smtp_host.strip(),
            "REPORT_SMTP_PORT": str(int(smtp_port)), "REPORT_SMTP_AUTH_CODE": smtp_auth_code.strip(),
            "REPORT_QUERY_ENABLED": str(report_query_enabled).lower(), "REPORT_IMAP_HOST": imap_host.strip(),
            "REPORT_IMAP_PORT": str(int(imap_port)), "REPORT_QUERY_INTERVAL_SECONDS": str(int(query_interval)),
        }
        for key, value in values.items():
            save_env_value(key, value)
            os.environ[key] = value
        st.success("已写入 .env。请重启后台服务以应用录音、转写和分析参数。")
    with st.expander("高级：编辑完整 .env 配置文档", expanded=False):
        document = st.text_area(".env", value=read_env_document(), height=360, key="env-document")
        if st.button("保存完整配置文档"):
            try:
                save_env_document(document)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(".env 已保存。请重启后台服务以加载新的配置。")
