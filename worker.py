from __future__ import annotations

import logging
import json
import signal
import shutil
import subprocess
import time
import socket
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
from rating_templates import rating_instruction
from storage import (
    add_segment,
    init_db,
    period_transcript,
    claim_segments,
    remove_incomplete_segment_records,
    requeue_interrupted_segments,
    requeue_dashscope_waiting_segments,
    save_period_summary,
    save_summary_version,
    update_segment,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
STOP = False
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
CONVERSATION_SYSTEM_PROMPT = """你是录音对话整理助手。只依据给定逐字稿整理对话主题、说话人、人物关系和事件。

严格规则：
1. 不得编造人物姓名、身份、关系、动机、感情状况、背景或未出现的事实。
2. 优先使用录音中明确出现的姓名、称呼、职位。没有充分依据时使用“说话人 A / B / C”。
3. 仅当上下文有明确证据时，才可使用“客户”“负责人”“同事”等角色，并在 evidence 中说明依据和 confidence。
4. 对“听说”“好像”“有人说”等内容，必须标为“转述传闻”；不得当作事实。多人同时说话或归属不明时标为“多人/不明”。
5. 日常闲聊优先识别值得关注的事件、关键人物、明确提到的关系和背景；只保留有原话依据的内容。
6. 不得根据语气、说话风格或出现频率猜测真实身份、私人关系或敏感背景。
7. 输出简洁、客观的中文 JSON，不输出 Markdown 或额外说明。"""
CONVERSATION_USER_PROMPT = """请分析以下一段录音逐字稿。时间戳和“原始说话人”来自语音转写，仅用于整理对话轮次。

严格按以下 JSON 结构输出：
{
  "title": "不超过 16 个汉字的录音标题；有实质内容时概括最主要话题，没有实质内容时必须为“空”",
  "overview": "100 字以内的整体摘要",
  "excitement_rating": "1 到 5 的整数。1=教学或纯工作讲解；2=日常聊天；3=明确人物关系、背景或动态；4=明确的矛盾、争吵、个人习惯或值得重点关注的内容；5=有充分原话依据的超级劲爆重大事件。只按录音证据评分，不得为了吸引眼球抬高分数。",
  "highlight_quotes": ["评分 3 星及以上时，从逐字稿原文摘录 1 到 3 段不超过 30 字的关键原话；必须逐字匹配原文。1-2 星返回空数组。"],
  "speaker_map": [
    {
      "source_speaker": "原始说话人编号；没有编号则省略",
      "display_name": "说话人 A 或有依据的角色/称呼",
      "evidence": "命名或角色判断的录音依据；没有依据写记录不充分",
      "confidence": "high|medium|low"
    }
  ],
  "noteworthy_events": [
    {
      "title": "事件标题",
      "type": "明确事实|转述传闻|信息不足",
      "details": "具体聊了什么",
      "people": ["相关人物"],
      "excitement_rating": "该事件的 1 到 5 星评分，遵循整体评分标准",
      "evidence_quote": "不超过 30 字的原话",
      "confidence": "high|medium|low",
      "caution": "传闻或不充分信息的必要说明"
    }
  ],
  "key_people": [
    {
      "name_or_label": "姓名或角色标签",
      "background": "录音明确提到的背景、职责或现状",
      "relationships": [
        {
          "with": "相关人物",
          "relationship": "明确提到的关系",
          "basis": "录音依据",
          "confidence": "high|medium|low"
        }
      ]
    }
  ],
  "topics": [
    {
      "title": "话题标题",
      "summary": "一句话概括",
      "participants": ["参与者"],
      "detailed_discussion": [
        {"point": "聊了什么", "details": "具体讨论内容"},
        {"point": "分歧与理由", "details": "没有则写未明确出现"},
        {"point": "最终安排", "details": "没有则写未形成明确结论"}
      ],
      "outcome": "结论或未形成明确结论",
      "action_items": [
        {"task": "明确的待办", "owner": "明确负责人或未明确", "due_date": "明确日期或未明确", "evidence": "原话依据"}
      ]
    }
  ]
}

逐字稿如下：
"""


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


def convert_to_mp3(segment_id: int, path: Path) -> Path:
    if path.suffix.lower() == ".mp3":
        return path
    target = path.with_suffix(".mp3")
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(path), "-codec:a", "libmp3lame", "-b:a", "48k", str(target),
    ]
    subprocess.run(command, check=True, timeout=3600, creationflags=subprocess.CREATE_NO_WINDOW)
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("MP3 转换没有生成有效文件。")
    update_segment(segment_id, path=str(target))
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


def conversation_source(transcript: str | None, raw_timeline: str | None) -> str:
    transcript = transcript.strip() if isinstance(transcript, str) else ""
    if not raw_timeline:
        return transcript
    try:
        timeline = json.loads(raw_timeline)
    except (TypeError, ValueError):
        return transcript
    if not isinstance(timeline, list):
        return transcript
    lines: list[str] = []
    for item in timeline:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        try:
            seconds = max(0, round(float(item["start_seconds"])))
        except (KeyError, TypeError, ValueError):
            continue
        minutes, seconds = divmod(seconds, 60)
        speaker = f"原始说话人 {item['speaker']}" if item.get("speaker") is not None else "原始说话人不明"
        lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}：{item['text'].strip()}")
    return "\n".join(lines) or transcript


def _json_object(content: str) -> dict[str, object]:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else ""
        content = content.rsplit("```", 1)[0].strip()
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("DeepSeek 未返回对象格式的对话分析。")
    return result


def _excitement_rating(raw_analysis: str | None) -> int:
    if not raw_analysis:
        return 1
    try:
        value = json.loads(raw_analysis).get("excitement_rating", 1)
        return min(5, max(0, int(value)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 1


def analyze_transcript_conversation(transcript: str | None, raw_timeline: str | None) -> dict[str, object]:
    source = conversation_source(transcript, raw_timeline)
    if not source:
        raise ValueError("该录音还没有可用的逐字稿，无法生成对话分析。")
    if not settings.llm_key:
        raise RuntimeError("未配置 DeepSeek API Key。")
    from models import active_chat_model

    response = requests.post(
        settings.llm_url,
        headers={"Authorization": f"Bearer {settings.llm_key}", "Content-Type": "application/json"},
        json={
            "model": active_chat_model(),
            "messages": [
                {"role": "system", "content": CONVERSATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        CONVERSATION_USER_PROMPT
                        + "\n\n以下评分规则覆盖上方 JSON 示例中的所有星级说明：\n"
                        + rating_instruction(settings.rating_template, settings.rating_custom_prompt)
                        + "\n请将整体和每个事件的 excitement_rating 都按此规则输出 0 到 5 的整数。\n\n"
                        + source
                    ),
                },
            ],
            "temperature": 0.2,
        },
        timeout=600,
    )
    response.raise_for_status()
    return _json_object(response.json()["choices"][0]["message"]["content"])


def summarize(transcript: str, period_label: str, selected_mode: str | None = None) -> tuple[str, str]:
    """Summarize with the selected model, falling back to the other available ones.

    The catalog comes from the provider, so a renamed model (for example
    deepseek-v4-flash -> deepseek-flash) does not break summaries.
    """
    from models import fallback_modes, resolve, resolve_key

    mode = resolve_key(selected_mode or settings.default_summary_mode) or ""
    errors: list[str] = []
    for candidate in dict.fromkeys([mode, *fallback_modes(mode)]):
        resolved = resolve(candidate)
        if resolved is None:
            continue
        label, provider, model = resolved
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
    save_summary_version(period_type, start, summary, provider, source_count)
    return summary, provider


def process(segment_id: int, path: Path) -> None:
    if not path.exists():
        update_segment(segment_id, status="failed", error="本地音频文件不存在。", lease_owner=None, lease_expires_at=None)
        return
    try:
        dbfs, active_seconds = audio_activity(path)
    except (OSError, RuntimeError):
        # Some valid imported formats (for example M4A or WMA) are decoded by
        # FFmpeg but not by the local SoundFile build used for silence detection.
        path = convert_to_mp3(segment_id, path)
        dbfs, active_seconds = audio_activity(path)
    with sf.SoundFile(path) as audio:
        duration_seconds = len(audio) / audio.samplerate
    update_segment(segment_id, dbfs=dbfs, active_seconds=active_seconds, duration_seconds=duration_seconds)
    if active_seconds < settings.min_active_seconds:
        silent_path = SILENT_DIR / path.name
        shutil.move(str(path), silent_path)
        update_segment(segment_id, path=str(silent_path), status="silent", lease_owner=None, lease_expires_at=None)
        return
    if settings.stt_provider != "dashscope_filetrans":
        update_segment(segment_id, status="awaiting_dashscope_config", error="当前仅启用百炼文件转写。", lease_owner=None, lease_expires_at=None)
        return
    if not bailian_is_configured():
        update_segment(segment_id, status="awaiting_dashscope_config", error="未配置百炼 API Key；本地音频已保留。", lease_owner=None, lease_expires_at=None)
        return
    mp3_path = convert_to_mp3(segment_id, path)
    result = bailian_transcribe(mp3_path)
    timeline = json.dumps(result.timeline, ensure_ascii=False) if result.timeline else None
    conversation_analysis = None
    conversation_analysis_error = None
    if settings.llm_key:
        try:
            conversation_analysis = json.dumps(
                analyze_transcript_conversation(result.transcript, timeline), ensure_ascii=False
            )
        except Exception as exc:
            conversation_analysis_error = str(exc)
            logging.exception("Could not analyze transcript %s", mp3_path.name)
    update_segment(
        segment_id,
        status="complete",
        transcript=result.transcript,
        transcript_timeline=timeline,
        conversation_analysis=conversation_analysis,
        conversation_analysis_error=conversation_analysis_error,
        excitement_rating=_excitement_rating(conversation_analysis),
        error=None,
        lease_owner=None,
        lease_expires_at=None,
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


def stop(*_: object) -> None:
    global STOP
    STOP = True


def main() -> None:
    ensure_storage()
    init_db()
    remove_incomplete_segment_records()
    requeue_interrupted_segments()
    if bailian_is_configured():
        requeue_dashscope_waiting_segments()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    owner = f"{socket.gethostname()}:{__import__('os').getpid()}"
    while not STOP:
        discover_recordings()
        jobs = claim_segments(owner=owner)
        for job in jobs:
            if STOP:
                break
            path = Path(job["path"])
            try:
                process(int(job["id"]), path)
            except Exception as exc:
                attempts = job["attempts"]
                if attempts >= 5:
                    target = FAILED_DIR / path.name
                    if path.exists():
                        shutil.move(str(path), target)
                        path = target
                    update_segment(int(job["id"]), path=str(path), status="failed", attempts=attempts,
                                   error=str(exc), lease_owner=None, lease_expires_at=None)
                else:
                    update_segment(int(job["id"]), status="queued", attempts=attempts, error=str(exc),
                                   lease_owner=None, lease_expires_at=None)
                logging.exception("Could not process %s", job["path"])
        if not STOP:
            time.sleep(5 if jobs else 15)


if __name__ == "__main__":
    main()
