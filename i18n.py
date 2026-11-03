from __future__ import annotations

"""Minimal UI translation layer.

The dashboard defaults to Simplified Chinese; English covers the navigation and
the primary controls. Missing keys fall back to the key itself, so a partially
translated screen never breaks.
"""

import os

from config import save_env_value
from i18n_ui import UI_STRINGS

LANGUAGES: dict[str, str] = {"zh": "简体中文", "en": "English"}

STRINGS: dict[str, dict[str, str]] = {
    "app.title": {"zh": "录音档案", "en": "Voice Recorder"},
    "tab.home": {"zh": "首页", "en": "Home"},
    "tab.archive": {"zh": "搜索与档案", "en": "Search & Archive"},
    "tab.settings": {"zh": "设置", "en": "Settings"},
    "tab.help": {"zh": "使用说明", "en": "Help"},
    "language.label": {"zh": "语言", "en": "Language"},
    "tab.analysis": {"zh": "对话分析", "en": "Analysis"},
    "tab.transcript": {"zh": "逐字稿与音频", "en": "Transcript & audio"},
    "status.queued": {"zh": "等待处理", "en": "Queued"},
    "status.transcribing": {"zh": "转写中", "en": "Transcribing"},
    "status.complete": {"zh": "已完成", "en": "Completed"},
    "status.silent": {"zh": "已过滤静音", "en": "Silent"},
    "status.failed": {"zh": "处理失败", "en": "Failed"},
    "status.awaiting_dashscope_config": {"zh": "等待转写配置", "en": "Awaiting API key"},
    "update.available": {"zh": "发现新版本", "en": "Update available"},
    "update.now": {"zh": "立即升级", "en": "Update now"},
    "update.check": {"zh": "检查更新", "en": "Check for updates"},
    "update.current": {"zh": "当前版本", "en": "Current version"},
    "update.latest": {"zh": "已是最新版本", "en": "You are up to date"},
    # Home
    "home.disk": {"zh": "磁盘可用", "en": "Disk free"},
    "home.database": {"zh": "数据库", "en": "Database"},
    "home.recorder": {"zh": "录音器", "en": "Recorder"},
    "home.worker": {"zh": "处理器", "en": "Processor"},
    "home.pause": {"zh": "暂停录音", "en": "Pause"},
    "home.resume": {"zh": "继续录音", "en": "Resume"},
    "home.valid": {"zh": "有效录音", "en": "Recordings"},
    "home.done": {"zh": "已完成", "en": "Transcribed"},
    "home.pending": {"zh": "等待处理", "en": "Pending"},
    "home.empty": {"zh": "无内容", "en": "Empty"},
    "home.summary": {"zh": "时段汇总", "en": "Summary"},
    "home.scope": {"zh": "汇总范围", "en": "Range"},
    "home.period.segments": {"zh": "录音分段", "en": "Segments"},
    "home.period.duration": {"zh": "录音时长", "en": "Recorded"},
    "home.period.speech": {"zh": "检测到说话", "en": "Speech"},
    "home.period.transcripts": {"zh": "可分析逐字稿", "en": "Transcripts"},
    "home.generate": {"zh": "生成汇总", "en": "Generate"},
    "home.model": {"zh": "模型", "en": "Model"},
    "home.breakdown": {"zh": "时段分布", "en": "By time of day"},
    "home.daily": {"zh": "每日明细", "en": "Daily breakdown"},
    "home.recent": {"zh": "本日和最近录音", "en": "Today & recent"},
    "home.show_empty": {"zh": "显示无内容录音", "en": "Show empty recordings"},
    # Archive
    "archive.title": {"zh": "全部录音", "en": "All recordings"},
    "archive.favorites": {"zh": "收藏夹", "en": "Favorites"},
    "archive.import": {"zh": "导入已有录音进行分析", "en": "Import existing audio"},
    "archive.cleanup": {"zh": "清理无内容录音", "en": "Clean up empty recordings"},
    "archive.batch": {"zh": "批量操作", "en": "Batch actions"},
    "archive.keyword": {"zh": "关键词", "en": "Keyword"},
    "archive.status": {"zh": "状态", "en": "Status"},
    "archive.rating": {"zh": "星级", "en": "Rating"},
    "archive.source": {"zh": "来源", "en": "Source"},
    "archive.show_empty": {"zh": "显示无内容录音（静音 / 转写失败）", "en": "Show empty recordings (silent / failed)"},
    # Help
    "help.title": {"zh": "使用说明", "en": "Help"},
    "about.title": {"zh": "关于 / About", "en": "About"},
}


def current_language() -> str:
    value = os.getenv("UI_LANGUAGE", "zh").strip().lower()
    return value if value in LANGUAGES else "zh"


def set_language(code: str) -> None:
    if code not in LANGUAGES:
        return
    save_env_value("UI_LANGUAGE", code)
    os.environ["UI_LANGUAGE"] = code


def t(key: str, **kwargs: object) -> str:
    entry = STRINGS.get(key) or UI_STRINGS.get(key)
    if not entry:
        return key
    text = entry.get(current_language()) or entry.get("zh") or key
    return text.format(**kwargs) if kwargs else text
