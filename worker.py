from __future__ import annotations

import logging
import json
import shutil
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import requests
import soundfile as sf
from dashscope import Generation

from bailian_filetrans import is_configured as bailian_is_configured
from bailian_filetrans import transcribe as bailian_transcribe
from config import FAILED_DIR, RECORDINGS_DIR, SILENT_DIR, ensure_storage, settings
from storage import (
    add_segment,
    init_db,
    period_transcript,
    queued_segments,
    remove_incomplete_segment_records,
    requeue_dashscope_waiting_segments,
    save_period_summary,
    update_segment,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
SUMMARY_SYSTEM_PROMPT = """你是一位客观、克制、善于梳理办公室日常对话的记录员。
只依据给定逐字稿陈述事实；不得猜测动机、关系、身份或未被明确说出的内容，不得夸大冲突，也不得输出私人敏感信息。
文字可以轻松自然，但不得用侮辱、诽谤或煽动性措辞。对来源不足或语义不清的信息，要明确标注“记录不充分”而非补全。"""
SUMMARY_USER_PROMPT = """以下是 {period_label} 的办公室录音逐字稿。请严格按以下四个板块输出 Markdown：

## 时段概要
用约 150 字概括该时段明确发生的整体情况。

## 最劲爆焦点
只选择一项最值得关注、且证据最充分的事件。给出短标题、事实描述和一段不超过 30 字的关键原话；没有可靠事件时明确说明。

## 离奇/吃瓜事件簿
只列出记录中确有依据、明显不寻常或值得关注的事件。每项包含一个简短标题、事实描述和一段不超过 30 字的关键原话。没有可靠依据时请写“未发现记录明确支持的异常事件”。

## 关键人物动态
仅列出逐字稿中被明确提及或发言频繁的人物（无法确认姓名时使用“说话人 A”等中性称呼），说明其明确活动或待办，不推断私人关系。

逐字稿如下：
"""
SUMMARY_MODES = {
    "deepseek_v4_flash": ("DeepSeek-V4-Flash", "deepseek", "deepseek-v4-flash"),
    "deepseek_v4_pro": ("DeepSeek-V4-Pro", "deepseek", "deepseek-v4-pro"),
    "qwen_plus": ("千问 Qwen Plus", "bailian", "qwen-plus"),
}


def audio_activity(path: Path) -> tuple[float, float]:
    squares = 0.0
    samples = 0
    active_samples = 0
    threshold = 10 ** (settings.silence_threshold_dbfs / 20)
    with sf.SoundFile(path) as audio:
        for block in audio.blocks(blocksize=8192, dtype="float32", always_2d=True):
            mono = np.mean(block, axis=1)
            squares += float(np.dot(mono, mono))
            samples += len(mono)
            active_samples += int(np.count_nonzero(np.abs(mono) >= threshold))
        sample_rate = audio.samplerate
    rms = (squares / samples) ** 0.5 if samples else 0.0
    return float(20 * np.log10(max(rms, 1e-12))), active_samples / sample_rate


def convert_to_mp3(path: Path) -> Path:
    if path.suffix.lower() == ".mp3":
        return path
    target = path.with_suffix(".mp3")
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(path), "-codec:a", "libmp3lame", "-b:a", "48k", str(target),
    ]
    subprocess.run(command, check=True, timeout=3600, creationflags=subprocess.CREATE_NO_WINDOW)
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("MP3 转换没有生成有效文件。")
    update_segment(path, path=str(target))
    path.unlink()
    return target


def summarize_with_bailian(transcript: str, period_label: str, model: str) -> str:
    response = Generation.call(
        model=model,
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": SUMMARY_USER_PROMPT.format(period_label=period_label) + transcript},
        ],
        result_format="message",
        temperature=0.3,
        api_key=settings.dashscope_api_key,
        workspace=settings.dashscope_workspace or None,
    )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(getattr(response, "message", "百炼分析请求失败。"))
    choices = getattr(getattr(response, "output", None), "choices", [])
    if not choices:
        raise RuntimeError("百炼分析未返回文本。")
    return choices[0].message.content


def summarize_with_deepseek(transcript: str, period_label: str, model: str) -> str:
    if not settings.llm_key:
        raise RuntimeError("未配置 DeepSeek API Key。")
    response = requests.post(
        settings.llm_url,
        headers={"Authorization": f"Bearer {settings.llm_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": SUMMARY_USER_PROMPT.format(period_label=period_label) + transcript},
            ],
            "temperature": 0.3,
        },
        timeout=300,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def summarize(transcript: str, period_label: str, selected_mode: str | None = None) -> tuple[str, str]:
    mode = selected_mode or settings.default_summary_mode
    fallback_order = [mode, "deepseek_v4_pro", "qwen_plus"]
    errors: list[str] = []
    for candidate in dict.fromkeys(fallback_order):
        label, provider, model = SUMMARY_MODES.get(candidate, SUMMARY_MODES["deepseek_v4_flash"])
        try:
            if provider == "deepseek" and settings.llm_key:
                return summarize_with_deepseek(transcript, period_label, model), label
            if provider == "bailian" and settings.dashscope_api_key:
                return summarize_with_bailian(transcript, period_label, model), label
        except Exception as exc:
            errors.append(f"{label}：{exc}")
    raise RuntimeError("；".join(errors) or "没有可用的文本分析 API。")


def generate_period_summary(
    period_type: str, start: datetime, end: datetime, period_label: str, selected_mode: str | None = None
) -> tuple[str, str]:
    transcript, source_count = period_transcript(start, end)
    if not transcript:
        raise ValueError("该时段没有可用于分析的逐字稿。")
    summary, provider = summarize(transcript, period_label, selected_mode)
    save_period_summary(period_type, start, summary, provider, source_count)
    return summary, provider


def process(path: Path) -> None:
    if not path.exists():
        update_segment(path, status="failed", error="本地音频文件不存在。")
        return
    dbfs, active_seconds = audio_activity(path)
    update_segment(path, dbfs=dbfs, active_seconds=active_seconds)
    if active_seconds < settings.min_active_seconds:
        silent_path = SILENT_DIR / path.name
        shutil.move(str(path), silent_path)
        update_segment(path, path=str(silent_path), status="silent")
        return
    if settings.stt_provider != "dashscope_filetrans":
        update_segment(path, status="awaiting_dashscope_config", error="当前仅启用百炼文件转写。")
        return
    if not bailian_is_configured():
        update_segment(path, status="awaiting_dashscope_config", error="未配置百炼 API Key；本地音频已保留。")
        return
    mp3_path = convert_to_mp3(path)
    update_segment(mp3_path, status="transcribing", error=None)
    result = bailian_transcribe(mp3_path)
    analysis = json.dumps(result.speech_analysis, ensure_ascii=False) if result.speech_analysis else None
    timeline = json.dumps(result.timeline, ensure_ascii=False) if result.timeline else None
    update_segment(
        mp3_path,
        status="complete",
        transcript=result.transcript,
        transcript_timeline=timeline,
        speech_analysis=analysis,
        error=None,
    )
    logging.info("Transcribed %s", mp3_path.name)


def discover_recordings() -> None:
    for pattern in ("*.wav", "*.mp3"):
        for path in RECORDINGS_DIR.glob(pattern):
            if path.name.endswith(".part.wav"):
                continue
            try:
                with sf.SoundFile(path) as audio:
                    add_segment(path, datetime.fromtimestamp(path.stat().st_mtime).astimezone(), len(audio) / audio.samplerate)
            except RuntimeError:
                logging.warning("Could not inspect recording %s", path)


def main() -> None:
    ensure_storage()
    init_db()
    remove_incomplete_segment_records()
    if bailian_is_configured():
        requeue_dashscope_waiting_segments()
    while True:
        discover_recordings()
        jobs = queued_segments()
        for job in jobs:
            path = Path(job["path"])
            try:
                process(path)
            except Exception as exc:
                attempts = job["attempts"] + 1
                if attempts >= 5:
                    target = FAILED_DIR / path.name
                    if path.exists():
                        shutil.move(str(path), target)
                        path = target
                    update_segment(Path(job["path"]), path=str(path), status="failed", attempts=attempts, error=str(exc))
                else:
                    update_segment(Path(job["path"]), attempts=attempts, error=str(exc))
                logging.exception("Could not process %s", job["path"])
        time.sleep(5 if jobs else 15)


if __name__ == "__main__":
    main()
