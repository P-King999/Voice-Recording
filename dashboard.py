from __future__ import annotations

import io
import json
import hmac
import os
import re
import shutil
from datetime import datetime, time, timedelta
from html import escape
from pathlib import Path
from uuid import uuid4

import numpy as np
import soundfile as sf
import streamlit as st
import sounddevice as sd

from config import (
    RECORDINGS_DIR,
    ROOT,
    dashboard_access_mode,
    ensure_storage,
    read_env_document,
    save_env_document,
    save_env_value,
    settings,
)
from exports import markdown as export_markdown, srt as export_srt, word_html
from health import snapshot
import maintenance
import models as model_catalog
import i18n
import report_book as book
from i18n import t
from rating_templates import (
    delete_user_template,
    rating_instruction,
    rating_templates,
    resolve_template_id,
    save_user_template,
    template_label,
)
from storage import (
    dashboard_counts,
    dashboard_overview,
    dashboard_segment,
    delete_segments,
    find_dashboard_overview,
    get_period_summary,
    init_db,
    list_segment_tags,
    list_speakers,
    period_transcript,
    add_segment,
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
APP_VERSION = "1.2.0"
RATING_LABELS = {
    0: "0星 无实质内容/未分析",
    1: "1星 低优先级",
    2: "2星 一般关注",
    3: "3星 需要关注",
    4: "4星 重要",
    5: "5星 紧急/重大",
}
SUPPORTED_AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".wma"}


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
        return min(5, max(0, int(value)))
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
    tags: tuple[str, ...],
    favorites_only: bool,
    newest_first: bool = True,
    page: int = 0,
    exclude_empty: bool = True,
    sources: tuple[str, ...] = (),
) -> tuple[list[dict[str, object]], int]:
    records, total = find_dashboard_overview(
        query, list(statuses), recorded_on, list(ratings), list(tags), favorites_only,
        newest_first, limit=100, offset=max(0, page) * 100,
        exclude_empty=exclude_empty, sources=list(sources) or None,
    )
    return [dict(record) for record in records], total


@st.cache_data(ttl=60, show_spinner=False)
def purge_preview(days: int) -> tuple[int, float]:
    plan = maintenance.plan_purge(days)
    return plan.count, plan.megabytes


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
        st.caption(t("u032"))
        for sentence in split_transcript(record["transcript"]):
            st.markdown(f'<div class="transcript-line">{transcript_html(sentence, fragments)}</div>', unsafe_allow_html=True)
        return

    st.caption(t("u001"))
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


def pending_playback_start(record_id: int) -> float | None:
    """Consume a sentence playback request for this recording, if one exists."""
    pending = st.session_state.get("pending_playback")
    if not isinstance(pending, dict) or pending.get("record_id") != record_id:
        return None
    st.session_state.pop("pending_playback", None)
    try:
        return max(0, float(pending["start_seconds"]))
    except (KeyError, TypeError, ValueError):
        return None


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
            st.info(t("u118"))
            return
        if not settings.llm_key:
            st.info(t("u119"))
            return
        if st.button(t("u033"), key=f"{widget_prefix}-analyze-{record['id']}"):
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
    if st.button(t("u002"), key=f"{widget_prefix}-rerate-{record['id']}"):
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

    st.markdown(t("u003"))
    st.write(text_value(analysis.get("overview")))

    st.markdown(t("u004"))
    events = object_list(analysis.get("noteworthy_events"))
    if not events:
        st.caption(t("u034"))
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

    st.markdown(t("u005"))
    people = object_list(analysis.get("key_people"))
    if not people:
        st.caption(t("u035"))
    for person in people:
        st.markdown(f"**{text_value(person.get('name_or_label'))}**")
        st.write(text_value(person.get("background")))
        for relationship in object_list(person.get("relationships")):
            st.caption(
                f"与 {text_value(relationship.get('with'))}：{text_value(relationship.get('relationship'))}"
                f"。依据：{text_value(relationship.get('basis'))}"
            )

    st.markdown(t("u006"))
    topics = object_list(analysis.get("topics"))
    if not topics:
        st.caption(t("u036"))
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


def model_labels() -> dict[str, str]:
    """Model choices come from the provider catalog, so renames need no code change."""
    return model_catalog.summary_mode_labels()


def model_index(labels: dict[str, str], wanted: str | None) -> int:
    key = model_catalog.resolve_key(wanted or settings.default_summary_mode)
    keys = list(labels)
    return keys.index(key) if key in keys else 0


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
    password = os.getenv("DASHBOARD_PASSWORD", "")
    bind_address = (os.getenv("DASHBOARD_BIND_ADDRESS") or settings.dashboard_bind_address).strip()
    local_no_password = os.getenv("DASHBOARD_LOCAL_NO_PASSWORD", "false").lower() == "true"
    access_mode = dashboard_access_mode(password, bind_address, local_no_password)

    if access_mode == "setup":
        st.subheader(t("u037"))
        st.caption(t("u038"))
        with st.form("dashboard-password-setup"):
            new_password = st.text_input(t("u120"), type="password")
            confirmed_password = st.text_input(t("u121"), type="password")
            submitted = st.form_submit_button(t("u122"))
        if submitted:
            if not new_password:
                st.error(t("u164"))
            elif new_password != confirmed_password:
                st.error(t("u175"))
            else:
                save_env_value("DASHBOARD_PASSWORD", new_password)
                os.environ["DASHBOARD_PASSWORD"] = new_password
                st.session_state["dashboard_password_initialized"] = True
                st.rerun()
        st.stop()

    if access_mode == "unprotected":
        return

    if st.session_state.get("authenticated"):
        return
    if st.session_state.pop("dashboard_password_initialized", False):
        st.success(t("u039"))
    st.subheader(t("u007"))
    with st.form("dashboard-login"):
        entered_password = st.text_input(t("u040"), type="password")
        submitted = st.form_submit_button(t("u041"))
    if submitted and hmac.compare_digest(entered_password, password):
        st.session_state["authenticated"] = True
        st.rerun()
    if submitted:
        st.error(t("u042"))
    st.stop()


def render_language_selector() -> None:
    """右上角只有一个小地球图标；完整的语言设置放在「设置」页。"""
    spacer, icon = st.columns([12, 1], vertical_alignment="center")
    with icon:
        with st.popover("🌐", help=t("language.label")):
            for code, name in i18n.LANGUAGES.items():
                active = code == i18n.current_language()
                if st.button(
                    ("✓ " if active else "") + name,
                    key=f"language-{code}",
                    disabled=active,
                    use_container_width=True,
                ):
                    i18n.set_language(code)
                    st.rerun()


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


def request_recorder_restart() -> None:
    """Ask the managed recorder to finish its current file and start with saved settings."""
    runtime_dir = ROOT / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    restart_path = runtime_dir / "recorder.restart"
    restart_path.touch()


def recorder_is_paused() -> bool:
    return (ROOT / "runtime" / "recorder.paused").exists()


def set_recorder_paused(paused: bool) -> None:
    runtime_dir = ROOT / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pause_path = runtime_dir / "recorder.paused"
    if paused:
        pause_path.touch()
    else:
        pause_path.unlink(missing_ok=True)


def _audio_files_at(source: Path) -> list[Path]:
    if source.is_file():
        return [source] if source.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES else []
    if not source.is_dir():
        return []
    return [path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES]


def import_audio_sources(source: Path) -> tuple[int, list[str]]:
    """Copy user-selected audio into managed storage and add it to the processing queue."""
    sources = _audio_files_at(source)
    if not sources:
        return 0, ["未找到支持的音频文件（WAV、MP3、FLAC、OGG、M4A、AAC、WMA）。"]
    imported_dir = RECORDINGS_DIR / "imported"
    imported_dir.mkdir(parents=True, exist_ok=True)
    imported = 0
    errors: list[str] = []
    for audio_path in sources:
        try:
            try:
                with sf.SoundFile(audio_path) as audio:
                    duration = len(audio) / audio.samplerate
            except RuntimeError:
                # FFmpeg in the worker will normalize formats not supported by libsndfile.
                duration = 0.0
            destination = imported_dir / f"{audio_path.stem}-{uuid4().hex[:8]}{audio_path.suffix.lower()}"
            shutil.copy2(audio_path, destination)
            add_segment(
                destination,
                datetime.fromtimestamp(audio_path.stat().st_mtime).astimezone(),
                duration,
                source="imported",
            )
            imported += 1
        except (OSError, RuntimeError) as exc:
            errors.append(f"{audio_path.name}: {exc}")
    return imported, errors


def request_dashboard_restart() -> None:
    """Ask the agent to restart only the dashboard after its response is sent."""
    runtime_dir = ROOT / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "dashboard.restart").touch()


def render_live_recording() -> None:
    active = active_recordings()
    if active:
        path, seconds = active[0]
        st.success(f"正在录音  |  {path.stem.replace('.part', '')}  |  已录 {seconds / 60:.1f} 分钟")
    elif health["services"].get("recorder"):
        st.info(t("u123"))
    else:
        st.warning(t("u124"))


def render_live_monitor() -> None:
    snapshot = live_audio_snapshot()
    if snapshot is None:
        st.warning(t("u043"))
        return
    path, samples, dbfs = snapshot
    signal_present = dbfs >= settings.silence_threshold_dbfs
    status = "正在录入声音" if signal_present else "当前环境安静"
    meter_value = int(min(100, max(0, (dbfs + 80) / 80 * 100)))
    monitor_cols = st.columns([1, 2])
    monitor_cols[0].metric(t("u008"), f"{dbfs:.1f} dBFS", status)
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
    tags = actions[1].text_input(t("u009"), value=current_tags, key=f"{detail_prefix}-tags-{record['id']}")
    notes = actions[2].text_input(t("u010"), value=str(record["notes"] or ""), key=f"{detail_prefix}-notes-{record['id']}")
    if actions[3].button(t("u011"), key=f"{detail_prefix}-save-{record['id']}"):
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
                start_seconds = pending_playback_start(int(record["id"]))
                st.audio(
                    str(audio_path),
                    format="audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav",
                    start_time=start_seconds or 0,
                    autoplay=start_seconds is not None,
                )
            st.caption(f"响度：{record['dbfs']:.1f} dBFS" if record["dbfs"] is not None else "等待音频分析")
        with right:
            if record["transcript"]:
                render_transcript(record, speaker_display_names(parse_conversation_analysis(record_value(record, "conversation_analysis")), list_speakers()), widget_prefix)
            elif record["error"]:
                st.error(str(record["error"]))
            else:
                st.info(t("u176"))


def record_title_or_reason(record: dict[str, object]) -> str:
    """A readable label for records that have no analysis title."""
    status = str(record.get("status"))
    if status == "silent":
        return "静音片段（没有检测到人声）"
    if status == "failed":
        return "转写失败（没有逐字稿）"
    if status == "awaiting_dashscope_config":
        return "等待转写配置"
    return "没有内容的录音"


def source_badge(record: dict[str, object]) -> str:
    """A compact marker so imported files are recognisable at a glance."""
    return " · 导入" if str(record.get("source") or "recorded") == "imported" else ""


def overview_title(record: dict[str, object]) -> str:
    """Prefer the analysis title without loading full analysis JSON for the home list."""
    title = str(record.get("display_title") or "").strip()
    if not title:
        analysis = parse_conversation_analysis(str(record.get("conversation_analysis") or ""))
        title = text_value(analysis.get("title"), "") if analysis else ""
    if title and title != "空":
        return title
    return record_title_or_reason(record)


def is_empty_record(record: dict[str, object]) -> bool:
    """True when the recording has no analysis title, i.e. nothing to read."""
    title = str(record.get("display_title") or "").strip()
    if not title:
        analysis = parse_conversation_analysis(str(record.get("conversation_analysis") or ""))
        title = text_value(analysis.get("title"), "") if analysis else ""
    return not title or title == "空"


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


def render_first_open_guide() -> None:
    if os.getenv("ONBOARDING_COMPLETED", "false").lower() == "true":
        return
    st.info(t("u012"))
    with st.form("onboarding-keys", clear_on_submit=False):
        st.markdown(t("u044"))
        key_left, key_right = st.columns(2)
        dashscope_key = key_left.text_input(
            t("u045"), type="password", placeholder="sk-..."
        )
        deepseek_key = key_right.text_input(
            t("u046"), type="password", placeholder="sk-..."
        )
        submitted = st.form_submit_button(t("u047"), type="primary")
    if submitted:
        saved = []
        if dashscope_key.strip():
            save_env_value("DASHSCOPE_API_KEY", dashscope_key.strip())
            os.environ["DASHSCOPE_API_KEY"] = dashscope_key.strip()
            saved.append("百炼")
        if deepseek_key.strip():
            save_env_value("LLM_API_KEY", deepseek_key.strip())
            os.environ["LLM_API_KEY"] = deepseek_key.strip()
            saved.append("DeepSeek")
        save_env_value("ONBOARDING_COMPLETED", "true")
        os.environ["ONBOARDING_COMPLETED"] = "true"
        if saved:
            request_recorder_restart()
            (ROOT / "runtime" / "worker.restart").touch()
            st.success(f"已保存{'、'.join(saved)}的 Key，处理服务正在重启。剩下的步骤请看下方说明。")
        else:
            st.success(t("u125"))
        st.rerun()
    st.caption(
        t("u013")
    )


def render_help_page() -> None:
    st.subheader(t("help.title"))
    with st.expander(t("u048"), expanded=True):
        st.write(t("u049"))
    with st.expander(t("u050")):
        st.write(t("u051"))
    with st.expander(t("u052")):
        st.write(t("u053"))
    with st.expander(t("u054")):
        st.write(t("u055"))
    with st.expander(t("u056")):
        st.write(t("u057"))
    with st.expander(t("u058")):
        st.write(t("u059"))
    with st.expander(t("u060")):
        st.markdown(
            f"""
**Voice Recorder** · v{APP_VERSION}

一个装在自己笔记本上的录音助手：它在后台记录你的声音，把没有声音的部分留在本地，
只把有内容的片段送去转写，再用大模型整理成可搜索的档案。没有账号，没有云端仓库，
音频和文字都在你自己的电脑里。

*Voice Recorder is a small self-hosted assistant that records your microphone in the
background, keeps silent audio on your machine and sends only meaningful clips to be
transcribed. Titles, summaries and star ratings are generated by an LLM, and everything
lands in a searchable local dashboard. No account, no cloud vault — your audio and
transcripts stay on your own laptop.*

- 项目主页 / Project: <https://github.com/P-King999/Voice-Recording>
- 更新日志 / Changelog: `CHANGELOG.md`
- 隐私 / Privacy: 录音与逐字稿仅保存在本机，只有需要转写的片段会上传云端。
"""
        )


render_language_selector()
st.title(t("app.title"))
render_first_open_guide()
home_tab, archive_tab, settings_tab, help_tab = st.tabs(
    [t("tab.home"), t("tab.archive"), t("tab.settings"), t("tab.help")]
)

with home_tab:
    health = snapshot(ROOT)
    health_cols = st.columns(4)
    health_cols[0].metric(t("home.disk"), f"{health['free_gb']:.1f} GB")
    health_cols[1].metric(t("home.database"), "正常" if health["database_ok"] else "异常")
    health_cols[2].metric(t("home.recorder"), "运行中" if health["services"].get("recorder") else "未运行")
    health_cols[3].metric(t("home.worker"), "运行中" if health["services"].get("worker") else "未运行")
    if health["free_gb"] < settings.disk_warning_gb:
        st.error(f"磁盘空间不足：仅剩 {health['free_gb']:.1f} GB，请及时归档或清理录音。")
    recording_action, recording_hint = st.columns([1, 5], vertical_alignment="center")
    if recorder_is_paused():
        if recording_action.button(t("home.resume"), type="primary", key="resume-recording"):
            set_recorder_paused(False)
            st.rerun()
        recording_hint.caption(t("u061"))
    else:
        if recording_action.button(t("home.pause"), key="pause-recording"):
            set_recorder_paused(True)
            st.rerun()
        recording_hint.caption(t("u062"))
    if hasattr(st, "fragment"):
        st.fragment(run_every=10)(render_live_recording)()
    else:
        render_live_recording()
    counts = dashboard_counts()
    metrics = st.columns(4)
    metrics[0].metric(t("home.valid"), counts.get("content", 0), help=t("u014"))
    metrics[1].metric(t("home.done"), counts.get("content_complete", 0))
    metrics[2].metric(t("home.pending"), counts.get("pending", 0))
    metrics[3].metric(t("home.empty"), counts.get("empty", 0), help=t("u015"))
    if counts.get("empty"):
        st.caption(
            f"另有 {counts['empty']} 条无内容录音（静音 / 转写失败）已从统计和列表隐藏，"
            f"{settings.empty_retention_days} 天后自动删除。"
        )
    st.subheader(t("home.summary"))
    now = datetime.now().astimezone()
    choice = st.segmented_control(t("u016"), ["本日", "本日上午", "本日下午", "本日晚上", "本周", "本月"], default="本日", key="home-summary-period")
    period_type, period_start, period_end, period_label = period_details(choice or "本日", now)
    period_stats = book.range_stats(period_start, min(period_end, now))

    cards = st.columns(4)
    cards[0].metric(t("home.period.segments"), f"{period_stats.segments} 个")
    cards[1].metric(t("home.period.duration"), book.hours_text(period_stats.recorded))
    cards[2].metric(t("home.period.speech"), book.hours_text(period_stats.active))
    cards[3].metric(t("home.period.transcripts"), f"{period_stats.transcribed} 段")
    if period_end <= period_start:
        st.caption(f"{period_label} 还没有开始。")
    else:
        st.caption(
            f"{period_label} · 统计范围 {period_start:%m-%d %H:%M} – {min(period_end, now):%m-%d %H:%M}"
        )

    summary = get_period_summary(period_type, period_start)
    summary_text = str(summary["summary"]).strip() if summary and summary["summary"] else ""
    summary_col, detail_col = st.columns([3, 2])
    with summary_col:
        with st.container(border=True):
            st.markdown(f"**{period_label}摘要**")
            if summary_text and summary_text not in {"空", "无"}:
                st.markdown(summary_text)
                st.caption(f"{summary['provider']} · 基于 {summary['source_segment_count']} 段逐字稿")
            else:
                st.caption(t("u165"))
            controls = st.columns([2, 1, 1])
            labels = model_labels()
            if st.session_state.get("home-summary-model") not in labels:
                st.session_state["home-summary-model"] = next(iter(labels), "qwen_plus")
            selected_mode = controls[0].selectbox(
                t("u126"), list(labels), format_func=lambda value: labels.get(value, value), key="home-summary-model"
            )
            if controls[1].button(t("u127"), key="home-generate-summary"):
                with st.spinner("正在读取该时段逐字稿并生成汇总..."):
                    transcript, source_count = period_transcript(period_start, period_end)
                    if not transcript:
                        st.warning(t("u183"))
                    else:
                        from worker import generate_period_summary
                        generate_period_summary(period_type, period_start, period_end, period_label, selected_mode)
                        st.success(f"已基于 {source_count} 段逐字稿生成汇总。")
                        st.rerun()
            if controls[2].button(t("u129"), key="home-refresh-models", help=t("u128")):
                try:
                    model_catalog.refresh(force=True)
                except Exception as exc:
                    st.warning(f"刷新失败：{exc}")
                st.rerun()
    with detail_col:
        with st.container(border=True):
            if period_type in {"day", "morning", "afternoon", "evening"}:
                st.markdown(t("u166"))
                day_card = book.day_stats(now.date(), now)
                st.dataframe(
                    [
                        {"时段": f"{label} {span}", "分段": count, "录音": book.hours_text(recorded), "说话": book.hours_text(active)}
                        for label, span, count, recorded, active in day_card.spans
                    ],
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.markdown(t("u167"))
                if period_stats.by_day:
                    st.dataframe(
                        [
                            {
                                "日期": f"{day:%m-%d}",
                                "分段": segments,
                                "录音": book.hours_text(recorded),
                                "说话": book.hours_text(active),
                                "逐字稿": f"{transcribed} 段",
                            }
                            for day, segments, recorded, active, transcribed in reversed(period_stats.by_day)
                        ],
                        hide_index=True,
                        width="stretch",
                        height=260,
                    )
                else:
                    st.caption(t("u177"))
    st.subheader(t("home.recent"))
    show_home_empty = st.toggle(
        t("home.show_empty"),
        key="home_show_empty",
        value=False,
        help=t("u017"),
    )
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
                    st.caption(t("u178"))
                visible_items = [item for item in items if not is_empty_record(item)][:8]
                empty_items = [item for item in items if is_empty_record(item)]
                for item in visible_items:
                    status = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
                    title, favorite_action, button = st.columns([4, 1, 1])
                    title.write(overview_title(item))
                    title.caption(f"{rating_label(item)}{source_badge(item)} · {record_recorded_at(item):%m-%d %H:%M} · {item['duration_seconds'] / 60:.1f} 分钟 · {status}")
                    if favorite_action.button("★" if item["favorite"] else "☆", key=f"{prefix}-favorite-{item['id']}", help="取消收藏" if item["favorite"] else "收藏"):
                        toggle_favorite(int(item["id"]), item["favorite"])
                        st.rerun()
                    if button.button(t("u136"), key=f"{prefix}-{item['id']}"):
                        st.session_state["home_record_id"] = int(item["id"])
                        st.rerun()
                if empty_items and show_home_empty:
                    silent = sum(1 for item in empty_items if str(item["status"]) == "silent")
                    failed = sum(1 for item in empty_items if str(item["status"]) == "failed")
                    parts = []
                    if silent:
                        parts.append(f"静音 {silent}")
                    if failed:
                        parts.append(f"失败 {failed}")
                    label = f"无内容录音 {len(empty_items)} 条（{'、'.join(parts) or '未分析'}）"
                    with st.expander(label, expanded=False):
                        st.caption(
                            t("u184")
                        )
                        st.dataframe(
                            [
                                {
                                    "时间": f"{record_recorded_at(item):%m-%d %H:%M}",
                                    "时长": f"{item['duration_seconds'] / 60:.1f} 分钟",
                                    "状态": STATUS_LABELS.get(str(item["status"]), str(item["status"])),
                                }
                                for item in empty_items[:50]
                            ],
                            hide_index=True,
                            width="stretch",
                        )
                        if len(empty_items) > 50:
                            st.caption(f"仅列出最近 50 条，共 {len(empty_items)} 条。")
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
        st.info(t("u063"))

with archive_tab:
    with st.expander(t("archive.import"), expanded=False):
        st.caption(t("u064"))
        source_path_text = st.text_input(t("u065"), placeholder=r"D:\\Meetings\\2026-08-20")
        if st.button(t("u066"), key="import-audio-path"):
            source_path = Path(source_path_text.strip()).expanduser()
            if not source_path_text.strip() or not source_path.exists():
                st.error(t("u168"))
            else:
                imported, errors = import_audio_sources(source_path)
                if imported:
                    clear_record_caches()
                    st.success(f"已导入 {imported} 个音频，正在排队转写与分析。")
                for error in errors[:5]:
                    st.warning(error)
        uploaded_audio = st.file_uploader(t("u067"), type=[suffix[1:] for suffix in sorted(SUPPORTED_AUDIO_SUFFIXES)], accept_multiple_files=True)
        if st.button(t("u068"), key="import-uploaded-audio", disabled=not uploaded_audio):
            upload_dir = ROOT / "runtime" / "uploaded-audio"
            upload_dir.mkdir(parents=True, exist_ok=True)
            uploaded_sources: list[Path] = []
            for uploaded in uploaded_audio:
                suffix = Path(uploaded.name).suffix.lower()
                temporary_path = upload_dir / f"{uuid4().hex}{suffix}"
                temporary_path.write_bytes(uploaded.getvalue())
                uploaded_sources.append(temporary_path)
            imported, errors = 0, []
            for uploaded_source in uploaded_sources:
                count, source_errors = import_audio_sources(uploaded_source)
                imported += count
                errors.extend(source_errors)
                uploaded_source.unlink(missing_ok=True)
            if imported:
                clear_record_caches()
                st.success(f"已导入 {imported} 个音频，正在排队转写与分析。")
            for error in errors[:5]:
                st.warning(error)
    archive_title, favorites_action = st.columns([6, 1])
    archive_title.subheader(t("archive.title"))
    if favorites_action.button(t("archive.favorites"), key="archive-favorites-toggle", type="secondary"):
        st.session_state["archive_favorites_only"] = not bool(st.session_state.get("archive_favorites_only", False))
        st.session_state["archive_page"] = 0
        st.session_state.pop("archive_record_id", None)
        st.rerun()
    if st.session_state.get("archive_favorites_only", False):
        st.caption(t("u069"))
    with st.form("archive-search", border=False):
        filter_cols = st.columns([3, 1, 2, 2, 2, 2, 1])
        query = filter_cols[0].text_input(t("u071"), placeholder=t("u070"))
        date_enabled = filter_cols[1].checkbox(t("u072"))
        filter_date = filter_cols[1].date_input(t("u073"), value=datetime.now().date(), disabled=not date_enabled)
        selected_statuses = filter_cols[2].multiselect(t("u074"), list(STATUS_LABELS), default=list(STATUS_LABELS), format_func=lambda value: STATUS_LABELS[value])
        selected_ratings = filter_cols[3].multiselect(t("u075"), list(range(6)), default=list(range(6)), format_func=lambda value: RATING_LABELS[value])
        selected_tags = filter_cols[4].multiselect(t("u009"), list_segment_tags(), placeholder=t("u076"))
        sort_order = filter_cols[5].selectbox(t("u077"), ["最近优先", "最早优先"])
        submitted = filter_cols[6].form_submit_button(t("u078"), use_container_width=True)
    if submitted or "archive_query" not in st.session_state:
        st.session_state["archive_query"] = query
        st.session_state["archive_statuses"] = tuple(selected_statuses)
        st.session_state["archive_date"] = filter_date if date_enabled else None
        st.session_state["archive_ratings"] = tuple(selected_ratings)
        st.session_state["archive_tags"] = tuple(selected_tags)
        st.session_state["archive_newest_first"] = sort_order == "最近优先"
        st.session_state["archive_page"] = 0
        st.session_state.pop("archive_record_id", None)
    page = int(st.session_state.get("archive_page", 0))
    results, total_results = search_records(
        st.session_state["archive_query"],
        st.session_state["archive_statuses"],
        st.session_state["archive_date"],
        st.session_state["archive_ratings"],
        st.session_state.get("archive_tags", ()),
        bool(st.session_state.get("archive_favorites_only", False)),
        bool(st.session_state["archive_newest_first"]),
        page,
        not bool(st.session_state.get("archive_show_empty", False)),
        {"全部": (), "录音": ("recorded",), "导入": ("imported",)}.get(
            st.session_state.get("archive_source", "全部"), ()
        ),
    )
    page_size = 100
    page_count = max(1, (total_results + page_size - 1) // page_size)
    archive_list_column, archive_detail_column = st.columns([5, 7], gap="large")
    archive_list_scroll = archive_list_column.container(height=720, border=False)
    archive_detail_scroll = archive_detail_column.container(height=720, border=False)
    with archive_detail_scroll:
        archive_record_id = st.session_state.get("archive_record_id")
        if archive_record_id:
            selected_record = load_record(int(archive_record_id))
            if selected_record:
                st.subheader(overview_title(selected_record))
                render_record_detail(selected_record, "archive")
            else:
                st.info(t("u169"))
        else:
            st.info(t("u130"))
    archive_list_scroll.caption(f"共 {total_results} 条录音，当前显示第 {page + 1} / {page_count} 页。")
    jump_date_column, jump_action_column, clear_jump_column = archive_list_scroll.columns([3, 1, 1])
    jump_date = jump_date_column.date_input(
        t("u019"),
        value=st.session_state.get("archive_date") or datetime.now().date(),
        key="archive-jump-date",
        help=t("u018"),
    )
    if jump_action_column.button(t("u020"), key="archive-jump-to-date", use_container_width=True):
        st.session_state["archive_date"] = jump_date
        st.session_state["archive_page"] = 0
        st.session_state.pop("archive_record_id", None)
        st.rerun()
    if clear_jump_column.button(
        t("u021"),
        key="archive-clear-date-jump",
        disabled=st.session_state.get("archive_date") is None,
        use_container_width=True,
    ):
        st.session_state["archive_date"] = None
        st.session_state["archive_page"] = 0
        st.session_state.pop("archive_record_id", None)
        st.rerun()
    if st.session_state.get("archive_date"):
        archive_list_scroll.caption(f"当前已定位至 {st.session_state['archive_date']:%Y年%m月%d日}。")

    if results:
        source_filter = (
            archive_list_scroll.segmented_control(
                t("u132"),
                ["全部", "录音", "导入"],
                default="全部",
                key="archive_source",
                help=t("u131"),
            )
            or "全部"
        )
        show_empty = archive_list_scroll.toggle(
            t("u080"),
            key="archive_show_empty",
            value=False,
            help=t("u079"),
        )
        visible_results = [item for item in results if not is_empty_record(item)]
        empty_results = [item for item in results if is_empty_record(item)]
        if not show_empty:
            archive_list_scroll.caption(
                f"已隐藏无内容录音（静音 / 转写失败），它们不计入统计，{settings.empty_retention_days} 天后自动删除。"
            )
        with archive_list_scroll.expander(t("archive.cleanup"), expanded=False):
            keep_days = st.number_input(
                t("u133"),
                min_value=1,
                max_value=365,
                value=int(settings.empty_retention_days),
                step=1,
                key="purge-keep-days",
            )
            purge_count, purge_mb = purge_preview(int(keep_days))
            st.caption(
                f"当前可清理 {purge_count} 条，约 {purge_mb:.0f} MB。"
                "只删除没有逐字稿、也没有分析标题的录音；有新内容的一律保留。"
            )
            purge_confirmed = st.checkbox(t("u134"), key="purge-confirm")
            if st.button(t("u135"), key="purge-now", disabled=not purge_confirmed or purge_count == 0):
                purged = maintenance.purge_empty_segments(int(keep_days))
                purge_preview.clear()
                st.success(f"已清理 {purged.count} 条，释放约 {purged.megabytes:.0f} MB。")
                clear_record_caches()
                st.rerun()
        visible_recording_date = None
        for item in visible_results:
            recording_at = record_recorded_at(item)
            if recording_at.date() != visible_recording_date:
                if visible_recording_date is not None:
                    archive_list_scroll.divider()
                archive_list_scroll.markdown(f"#### {recording_at:%Y年%m月%d日}")
                visible_recording_date = recording_at.date()
            status = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
            title, timestamp, favorite_action, action = archive_list_scroll.columns([5, 3, 1, 1])
            title.write(overview_title(item))
            timestamp.caption(f"{rating_label(item)}{source_badge(item)} · {record_recorded_at(item):%Y-%m-%d %H:%M} · {item['duration_seconds'] / 60:.1f} 分钟 · {status}")
            if favorite_action.button("★" if item["favorite"] else "☆", key=f"archive-favorite-{item['id']}", help="取消收藏" if item["favorite"] else "收藏"):
                toggle_favorite(int(item["id"]), item["favorite"])
                st.rerun()
            if action.button(t("u136"), key=f"archive-view-{item['id']}"):
                st.session_state["archive_record_id"] = int(item["id"])
                st.rerun()
        if empty_results and show_empty:
            with archive_list_scroll.expander(f"无内容录音（{len(empty_results)} 条）", expanded=False):
                for item in empty_results:
                    status = STATUS_LABELS.get(str(item["status"]), str(item["status"]))
                    title, timestamp, favorite_action, action = st.columns([5, 3, 1, 1])
                    title.write(overview_title(item))
                    timestamp.caption(f"{rating_label(item)}{source_badge(item)} · {record_recorded_at(item):%Y年%m月%d日 %H:%M} · {item['duration_seconds'] / 60:.1f} 分钟 · {status}")
                    if favorite_action.button("★" if item["favorite"] else "☆", key=f"archive-empty-favorite-{item['id']}", help="取消收藏" if item["favorite"] else "收藏"):
                        toggle_favorite(int(item["id"]), item["favorite"])
                        st.rerun()
                    if action.button(t("u136"), key=f"archive-empty-view-{item['id']}"):
                        st.session_state["archive_record_id"] = int(item["id"])
                        st.rerun()
        elif empty_results:
            silent = sum(1 for item in empty_results if str(item["status"]) == "silent")
            failed = sum(1 for item in empty_results if str(item["status"]) == "failed")
            parts = [f"静音 {silent}" if silent else "", f"失败 {failed}" if failed else ""]
            archive_list_scroll.caption(
                f"已隐藏 {len(empty_results)} 条无内容录音（{'、'.join(part for part in parts if part) or '未分析'}）。"
                "需要查看时打开上方的“显示无内容录音”。"
            )
        pager_left, pager_center, pager_right = archive_list_scroll.columns([1, 4, 1])
        if pager_left.button(t("u081"), disabled=page == 0):
            st.session_state["archive_page"] = page - 1
            st.rerun()
        pager_center.caption(f"第 {page + 1} 页，共 {page_count} 页")
        if pager_right.button(t("u082"), disabled=page + 1 >= page_count):
            st.session_state["archive_page"] = page + 1
            st.rerun()
    else:
        archive_list_scroll.info(t("u083"))

    with archive_list_scroll.expander(t("archive.batch"), expanded=False):
        selected_ids = st.multiselect(
            t("u084"),
            [int(item["id"]) for item in results],
            format_func=lambda item: overview_title(next(candidate for candidate in results if int(candidate["id"]) == item)),
        )
        selected_records = [load_record(item) for item in selected_ids]
        selected_records = [item for item in selected_records if item]
        actions = st.columns(4)
        if actions[0].button(t("u085"), disabled=not selected_records):
            for item in selected_records:
                update_segment(int(item["id"]), status="queued", attempts=0, error=None, lease_owner=None, lease_expires_at=None)
            clear_record_caches()
            st.rerun()
        if actions[1].button(t("u086"), disabled=not selected_records):
            st.session_state["confirm_delete"] = True
        if st.session_state.get("confirm_delete") and selected_records:
            if st.checkbox(t("u137")):
                import recycle

                recycle.recycle_paths([Path(str(item["path"])) for item in selected_records])
                delete_segments([int(item["id"]) for item in selected_records])
                st.session_state["confirm_delete"] = False
                clear_record_caches()
                st.rerun()
        actions[2].download_button(t("u087"), export_markdown(selected_records), "recordings.md", "text/markdown", disabled=not selected_records)
        actions[3].download_button(t("u088"), word_html(selected_records), "recordings.doc", "application/msword", disabled=not selected_records)
        st.download_button(t("u089"), export_srt(selected_records), "recordings.srt", "application/x-subrip", disabled=not selected_records)

with settings_tab:
    language_row = st.columns([2, 5], vertical_alignment="center")
    chosen_language = language_row[0].selectbox(
        t("language.label"),
        list(i18n.LANGUAGES),
        index=list(i18n.LANGUAGES).index(i18n.current_language()),
        format_func=lambda code: i18n.LANGUAGES[code],
        key="settings-language",
    )
    language_row[1].caption("界面语言保存在 .env 的 UI_LANGUAGE，切换后立即生效。")
    if chosen_language != i18n.current_language():
        i18n.set_language(chosen_language)
        st.rerun()
    device_section = st.expander(t("u022"), expanded=True)
    device_options, device_labels = input_device_options()
    selected_device = saved_recording_device()
    if selected_device not in device_options:
        device_options.append(selected_device)
        device_labels[selected_device] = f"已保存设备：{selected_device}（当前不可用）"
    device_choice = device_section.selectbox(
        t("u023"),
        device_options,
        index=device_options.index(selected_device),
        format_func=lambda item: device_labels[item],
    )
    device_actions = device_section.columns(2)
    if device_actions[0].button(t("u024")):
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
        device_section.audio(st.session_state["microphone_test_clip"], format="audio/wav")
        device_section.success(f"声音测试完成：{test_dbfs:.1f} dBFS" if test_dbfs >= settings.silence_threshold_dbfs else f"声音测试完成，但音量偏低：{test_dbfs:.1f} dBFS")
    if device_actions[1].button(t("u025")):
        try:
            if device_choice != "auto":
                sd.check_input_settings(
                    device=device_choice,
                    samplerate=settings.sample_rate,
                    channels=settings.channels,
                    dtype="float32",
                )
            save_env_value("RECORDING_DEVICE", device_choice)
            os.environ["RECORDING_DEVICE"] = device_choice
            request_recorder_restart()
        except Exception as exc:
            st.error(f"切换失败：{exc}")
        else:
            st.session_state["recording_device_changed"] = device_labels[device_choice]
            st.rerun()
    if changed_device := st.session_state.pop("recording_device_changed", None):
        device_section.success(f"已切换到 {changed_device}，录音器已重新启动。")
    if device_section.toggle(t("u026"), key="live_monitor_enabled"):
        if hasattr(st, "fragment"):
            st.fragment(run_every=1)(render_live_monitor)()
        else:
            render_live_monitor()

    st.divider()
    st.subheader(t("u027"))
    st.caption(t("u028"))
    reveal_secrets = st.checkbox(t("u029"), value=False)
    def env_value(key: str) -> str:
        return os.getenv(key, "")
    if st.button(t("u030")):
        save_env_value("DASHSCOPE_API_KEY", env_value("DASHSCOPE_API_KEY"))
        save_env_value("LLM_API_KEY", env_value("LLM_API_KEY"))
        st.success(t("u090"))
    st.info(t("u031"))
    available_rating_templates = rating_templates()
    saved_rating_template = resolve_template_id(env_value("RATING_TEMPLATE") or settings.rating_template)
    if saved_rating_template not in available_rating_templates:
        saved_rating_template = "general"
    with st.expander(t("u091"), expanded=True):
        rating_template = st.selectbox(
            t("u092"),
            list(available_rating_templates),
            index=list(available_rating_templates).index(saved_rating_template),
            format_func=template_label,
        )
        st.text_area(t("u093"), value=rating_instruction(rating_template), height=180, disabled=True)
        with st.popover(t("u138"), use_container_width=True):
            if st.button(t("u139"), key="copy-rating-template"):
                st.session_state["rating_template_draft_name"] = f"{template_label(rating_template)} 副本"
                st.session_state["rating_template_draft_prompt"] = rating_instruction(rating_template, settings.rating_custom_prompt)
                st.rerun()
            with st.form("create-rating-template", border=False):
                new_template_name = st.text_input(t("u170"), value=st.session_state.get("rating_template_draft_name", ""))
                new_template_prompt = st.text_area(t("u171"), value=st.session_state.get("rating_template_draft_prompt", ""), height=180)
                create_template = st.form_submit_button(t("u172"))
            if create_template:
                try:
                    template_id = save_user_template(None, new_template_name, new_template_prompt)
                except ValueError as exc:
                    st.error(str(exc))
                else:
                    st.session_state["rating_template_draft_name"] = ""
                    st.session_state["rating_template_draft_prompt"] = ""
                    st.session_state["rating_template_created"] = template_id
                    st.rerun()
            personal_templates = {key: item for key, item in available_rating_templates.items() if key.startswith("user_")}
            if personal_templates:
                selected_personal_template = st.selectbox(t("u173"), list(personal_templates), format_func=template_label, key="edit-personal-rating-template")
                current_personal = personal_templates[selected_personal_template]
                with st.form("edit-rating-template", border=False):
                    edited_name = st.text_input(t("u179"), value=current_personal["name"])
                    edited_prompt = st.text_area(t("u180"), value=current_personal["prompt"], height=180)
                    save_template, remove_template = st.columns(2)
                    save_template_clicked = save_template.form_submit_button(t("u181"))
                    remove_template_clicked = remove_template.form_submit_button(t("u182"))
                if save_template_clicked:
                    try:
                        save_user_template(selected_personal_template, edited_name, edited_prompt)
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        st.rerun()
                if remove_template_clicked:
                    delete_user_template(selected_personal_template)
                    st.rerun()
        if created_template := st.session_state.pop("rating_template_created", None):
            st.success(f"已新增“{template_label(created_template)}”，请选择它并保存全部设置。")
    with st.form("runtime-settings"):
        api_section = st.expander(t("u094"), expanded=True)
        api_left, api_right = api_section.columns(2)
        dashscope_key = api_left.text_input(t("u096"), value=env_value("DASHSCOPE_API_KEY"), type="default" if reveal_secrets else "password", help=t("u095"))
        llm_key = api_right.text_input("DeepSeek API Key", value=env_value("LLM_API_KEY"), type="default" if reveal_secrets else "password", help=t("u097"))
        recording_section = st.expander(t("u098"), expanded=True)
        recording_cols = recording_section.columns(3)
        segment_seconds = recording_cols[0].number_input(t("u099"), min_value=60, value=int(env_value("SEGMENT_SECONDS") or settings.segment_seconds), step=60)
        silence_threshold = recording_cols[1].number_input(t("u100"), value=float(env_value("SILENCE_THRESHOLD_DBFS") or settings.silence_threshold_dbfs), step=1.0)
        active_seconds = recording_cols[2].number_input(t("u101"), min_value=0.0, value=float(env_value("MIN_ACTIVE_SECONDS") or settings.min_active_seconds), step=0.5)
        recording_directory = recording_section.text_input(t("u103"), value=str(RECORDINGS_DIR), help=t("u102"))
        dashboard_section = st.expander(t("u104"), expanded=False)
        model_cols = dashboard_section.columns(2)
        summary_mode_labels = model_labels()
        summary_mode = model_cols[0].selectbox(
            t("u106"),
            list(summary_mode_labels),
            index=model_index(summary_mode_labels, env_value("DEFAULT_SUMMARY_MODE")),
            format_func=lambda value: summary_mode_labels.get(value, value),
            help=t("u105"),
        )
        dashboard_password = model_cols[1].text_input(t("u108"), value=env_value("DASHBOARD_PASSWORD"), type="default" if reveal_secrets else "password", help=t("u107"))
        dashboard_bind_address = (env_value("DASHBOARD_BIND_ADDRESS") or settings.dashboard_bind_address).strip()
        lan_access_enabled = dashboard_section.toggle(
            t("u110"),
            value=dashboard_bind_address == "0.0.0.0",
            help=t("u109"),
        )
        local_no_password = model_cols[1].toggle(
            t("u112"),
            value=(env_value("DASHBOARD_LOCAL_NO_PASSWORD") or str(settings.dashboard_local_no_password)).lower() == "true",
            disabled=lan_access_enabled,
            help=t("u111"),
        )
        warning_gb = dashboard_section.number_input(t("u113"), min_value=1.0, value=float(env_value("DISK_WARNING_GB") or settings.disk_warning_gb), step=1.0)
        st.divider()
        with st.expander(t("u140"), expanded=False):
            st.caption(t("u141"))
            report_enabled = st.checkbox(t("u142"), value=(env_value("REPORT_ENABLED") or str(settings.report_enabled)).lower() == "true")
            report_cols = st.columns(2)
            report_times = report_cols[0].text_input(t("u143"), value=env_value("REPORT_TIMES") or ",".join(settings.report_times), placeholder="12:10,18:10")
            report_recipient = report_cols[1].text_input(t("u144"), value=env_value("REPORT_EMAIL_RECIPIENT") or settings.report_email_recipient)
            report_channels = st.multiselect(
                t("u145"),
                ["smtp", "wecom", "pushplus", "feishu"],
                default=list(settings.report_channels),
                format_func={"smtp": "QQ 邮箱", "wecom": "企业微信机器人", "pushplus": "PushPlus", "feishu": "飞书机器人"}.get,
            )
            smtp_cols = st.columns(3)
            smtp_username = smtp_cols[0].text_input(t("u146"), value=env_value("REPORT_SMTP_USERNAME") or settings.report_smtp_username, placeholder="name@qq.com")
            smtp_host = smtp_cols[1].text_input(t("u147"), value=env_value("REPORT_SMTP_HOST") or settings.report_smtp_host)
            smtp_port = smtp_cols[2].number_input(t("u148"), min_value=1, max_value=65535, value=int(env_value("REPORT_SMTP_PORT") or settings.report_smtp_port), step=1)
            smtp_auth_code = st.text_input(t("u150"), value=env_value("REPORT_SMTP_AUTH_CODE"), type="password", help=t("u149"))
            st.markdown(t("u151"))
            feishu_cols = st.columns(2)
            feishu_app_id = feishu_cols[0].text_input(t("u152"), value=env_value("FEISHU_APP_ID"), type="default" if reveal_secrets else "password")
            feishu_chat_name = feishu_cols[1].text_input(t("u154"), value=env_value("FEISHU_CHAT_NAME"), placeholder=t("u153"))
            feishu_app_secret = st.text_input(t("u155"), value=env_value("FEISHU_APP_SECRET"), type="default" if reveal_secrets else "password")
            st.markdown(t("u156"))
            query_cols = st.columns(3)
            report_query_enabled = query_cols[0].checkbox(t("u157"), value=(env_value("REPORT_QUERY_ENABLED") or str(settings.report_query_enabled)).lower() == "true")
            imap_host = query_cols[1].text_input(t("u158"), value=env_value("REPORT_IMAP_HOST") or settings.report_imap_host)
            imap_port = query_cols[2].number_input(t("u159"), min_value=1, max_value=65535, value=int(env_value("REPORT_IMAP_PORT") or settings.report_imap_port), step=1)
            query_interval = st.number_input(t("u160"), min_value=20, value=int(env_value("REPORT_QUERY_INTERVAL_SECONDS") or settings.report_query_interval_seconds), step=10)
        st.caption(t("u114"))
        save_settings = st.form_submit_button(t("u115"), type="primary", use_container_width=True)
    if save_settings:
        new_dashboard_bind_address = "0.0.0.0" if lan_access_enabled else "127.0.0.1"
        if not dashboard_password.strip():
            st.error(t("u161"))
            st.stop()
        configured_directory = recording_directory.strip()
        recording_path = (ROOT / configured_directory).resolve() if configured_directory and not Path(configured_directory).is_absolute() else Path(configured_directory).expanduser().resolve() if configured_directory else ROOT / "data" / "recordings"
        try:
            recording_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            st.error(f"无法使用录音目录：{exc}")
            st.stop()
        recording_directory_changed = recording_path != RECORDINGS_DIR.resolve()
        values = {
            "DASHSCOPE_API_KEY": dashscope_key.strip(), "LLM_API_KEY": llm_key.strip(),
            "SEGMENT_SECONDS": str(int(segment_seconds)), "SILENCE_THRESHOLD_DBFS": str(silence_threshold),
            "MIN_ACTIVE_SECONDS": str(active_seconds), "DEFAULT_SUMMARY_MODE": summary_mode,
            "RATING_TEMPLATE": rating_template, "RATING_CUSTOM_PROMPT": "",
            "RECORDINGS_DIR": str(recording_path),
            "DASHBOARD_PASSWORD": dashboard_password, "DISK_WARNING_GB": str(warning_gb),
            "DASHBOARD_BIND_ADDRESS": new_dashboard_bind_address,
            "DASHBOARD_LOCAL_NO_PASSWORD": str(local_no_password and not lan_access_enabled).lower(),
            "REPORT_ENABLED": str(report_enabled).lower(), "REPORT_TIMES": report_times.strip(),
            "REPORT_CHANNELS": ",".join(report_channels), "REPORT_EMAIL_RECIPIENT": report_recipient.strip(),
            "REPORT_SMTP_USERNAME": smtp_username.strip(), "REPORT_SMTP_HOST": smtp_host.strip(),
            "REPORT_SMTP_PORT": str(int(smtp_port)), "REPORT_SMTP_AUTH_CODE": smtp_auth_code.strip(),
            "FEISHU_APP_ID": feishu_app_id.strip(), "FEISHU_APP_SECRET": feishu_app_secret.strip(),
            "FEISHU_CHAT_NAME": feishu_chat_name.strip(),
            "REPORT_QUERY_ENABLED": str(report_query_enabled).lower(), "REPORT_IMAP_HOST": imap_host.strip(),
            "REPORT_IMAP_PORT": str(int(imap_port)), "REPORT_QUERY_INTERVAL_SECONDS": str(int(query_interval)),
        }
        for key, value in values.items():
            save_env_value(key, value)
            os.environ[key] = value
        request_recorder_restart()
        if dashboard_bind_address != new_dashboard_bind_address or recording_directory_changed:
            request_dashboard_restart()
            st.success(t("u162"))
        else:
            st.success(t("u163"))
    with st.expander(t("u116"), expanded=False):
        document = st.text_area(".env", value=read_env_document(), height=360, key="env-document")
        if st.button(t("u117")):
            try:
                save_env_document(document)
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(t("u174"))

with help_tab:
    render_help_page()
