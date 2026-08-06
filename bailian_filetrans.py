from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import requests
from dashscope import Files
from dashscope.audio.qwen_asr.qwen_transcription import QwenTranscription

from config import settings


@dataclass(frozen=True)
class FileTranscription:
    transcript: str
    timeline: list[dict[str, object]]


def is_configured() -> bool:
    return bool(settings.dashscope_api_key)


def _as_data(value: object) -> object:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[no-any-return]
    if hasattr(value, "__dict__"):
        return vars(value)
    return value


def _uploaded_file_id(response: object) -> str:
    output = _as_data(getattr(response, "output", {}))
    if not isinstance(output, Mapping):
        raise RuntimeError("百炼未返回上传文件信息。")
    uploaded = output.get("uploaded_files") or output.get("files") or []
    if isinstance(uploaded, list) and uploaded:
        item = _as_data(uploaded[0])
        if isinstance(item, Mapping):
            for key in ("id", "file_id", "name", "url"):
                if item.get(key):
                    return str(item[key])
    for key in ("id", "file_id", "name", "url"):
        if output.get(key):
            return str(output[key])
    raise RuntimeError("百炼上传响应中没有文件 ID。")


def _temporary_file_url(file_id: str) -> str:
    response = Files.get(
        file_id=file_id,
        api_key=settings.dashscope_api_key,
        workspace=settings.dashscope_workspace or None,
    )
    if getattr(response, "status_code", 0) != 200:
        raise RuntimeError(f"百炼临时文件查询失败：{getattr(response, 'message', 'unknown')}")
    output = _as_data(getattr(response, "output", {}))
    if isinstance(output, Mapping) and isinstance(output.get("url"), str):
        return output["url"]
    raise RuntimeError("百炼未返回临时音频访问地址。")


def _collect_text(value: object, result_urls: list[str]) -> list[str]:
    value = _as_data(value)
    if isinstance(value, Mapping):
        texts: list[str] = []
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"text", "transcript", "transcription"} and isinstance(item, str):
                texts.append(item.strip())
            elif normalized.endswith("_url") and isinstance(item, str) and item.startswith("http"):
                result_urls.append(item)
            else:
                texts.extend(_collect_text(item, result_urls))
        return texts
    if isinstance(value, list):
        return [text for item in value for text in _collect_text(item, result_urls)]
    return []


def _timestamp_seconds(value: object, key: str) -> float | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp < 0:
        return None
    return timestamp if "second" in key else timestamp / 1000


def _sentence_entry(value: object) -> dict[str, object] | None:
    value = _as_data(value)
    if not isinstance(value, Mapping):
        return None
    text = next(
        (str(value[key]).strip() for key in ("text", "transcript", "content") if isinstance(value.get(key), str)),
        "",
    )
    start_key = next((key for key in ("begin_time", "begin_time_ms", "start_time", "start_time_ms") if key in value), None)
    if not text or start_key is None:
        return None
    start_seconds = _timestamp_seconds(value[start_key], start_key)
    if start_seconds is None:
        return None
    end_key = next((key for key in ("end_time", "end_time_ms", "stop_time", "stop_time_ms") if key in value), None)
    end_seconds = _timestamp_seconds(value[end_key], end_key) if end_key is not None else None
    sentence: dict[str, object] = {"text": text, "start_seconds": start_seconds}
    if end_seconds is not None and end_seconds >= start_seconds:
        sentence["end_seconds"] = end_seconds
    speaker = next((value[key] for key in ("speaker", "speaker_id", "speaker_name") if value.get(key) is not None), None)
    if speaker is not None:
        sentence["speaker"] = str(speaker)
    return sentence


def _collect_sentence_timeline(value: object) -> list[dict[str, object]]:
    value = _as_data(value)
    if isinstance(value, Mapping):
        timeline: list[dict[str, object]] = []
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"sentences", "sentence", "utterances", "utterance"}:
                candidates = item if isinstance(item, list) else [item]
                for candidate in candidates:
                    sentence = _sentence_entry(candidate)
                    if sentence:
                        timeline.append(sentence)
            timeline.extend(_collect_sentence_timeline(item))
        return timeline
    if isinstance(value, list):
        return [sentence for item in value for sentence in _collect_sentence_timeline(item)]
    return []


def _timeline_from_sources(sources: list[object]) -> list[dict[str, object]]:
    unique: dict[tuple[float, str], dict[str, object]] = {}
    for source in sources:
        for sentence in _collect_sentence_timeline(source):
            unique[(float(sentence["start_seconds"]), str(sentence["text"]))] = sentence
    return sorted(unique.values(), key=lambda sentence: float(sentence["start_seconds"]))


def _response_transcript(response: object) -> FileTranscription:
    result_urls: list[str] = []
    sources: list[object] = [getattr(response, "output", {})]
    texts = _collect_text(sources[0], result_urls)
    for url in dict.fromkeys(result_urls):
        downloaded = requests.get(url, timeout=60)
        downloaded.raise_for_status()
        try:
            body = downloaded.json()
            sources.append(body)
            texts.extend(_collect_text(body, []))
        except ValueError:
            texts.append(downloaded.text.strip())
    return FileTranscription(
        "\n".join(dict.fromkeys(text for text in texts if text)),
        _timeline_from_sources(sources),
    )


def transcribe(path: Path) -> FileTranscription:
    """Upload a local audio file to DashScope temporary storage and await file transcription."""
    if not is_configured():
        raise RuntimeError("未配置百炼 API Key。")
    uploaded = Files.upload(
        file_path=str(path),
        purpose="file-extract",
        api_key=settings.dashscope_api_key,
        workspace=settings.dashscope_workspace or None,
    )
    if getattr(uploaded, "status_code", 0) != 200:
        raise RuntimeError(f"百炼文件上传失败：{getattr(uploaded, 'message', 'unknown')}")
    file_id = _uploaded_file_id(uploaded)
    try:
        file_url = _temporary_file_url(file_id)
        submitted = QwenTranscription.async_call(
            model=settings.dashscope_asr_model,
            file_url=file_url,
            api_key=settings.dashscope_api_key,
            workspace=settings.dashscope_workspace or None,
            diarization_enabled=settings.dashscope_enable_diarization,
        )
        if getattr(submitted, "status_code", 0) != 200:
            raise RuntimeError(f"百炼转写任务提交失败：{getattr(submitted, 'message', 'unknown')}")
        completed = QwenTranscription.wait(
            submitted,
            api_key=settings.dashscope_api_key,
            workspace=settings.dashscope_workspace or None,
            wait_timeout=7200,
        )
        if getattr(completed, "status_code", 0) != 200:
            raise RuntimeError(f"百炼转写任务失败：{getattr(completed, 'message', 'unknown')}")
        completed_output = _as_data(getattr(completed, "output", {}))
        task_status = ""
        if isinstance(completed_output, Mapping):
            task_status = str(completed_output.get("task_status", "")).upper()
        if task_status not in {"SUCCESS", "SUCCEEDED"}:
            raise RuntimeError(f"百炼转写任务未成功完成：{task_status or 'UNKNOWN'}")
        return _response_transcript(completed)
    finally:
        if settings.dashscope_delete_uploaded_file:
            try:
                Files.delete(file_id=file_id, api_key=settings.dashscope_api_key, workspace=settings.dashscope_workspace or None)
            except Exception:
                pass
