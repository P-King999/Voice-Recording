from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

from config import DATA_DIR


TEMPLATES_PATH = DATA_DIR / "rating_templates.json"
BUILTIN_TEMPLATES = {
    "general": {
        "name": "通用重要度",
        "prompt": """按内容的重要程度从 0 到 5 星评分。0=没有实质内容或无法判断；1=普通闲聊、重复信息或常规讲解；2=有价值的背景、进展或需要知晓的信息；3=明确的决定、待办、负责人、时间点或会影响后续工作的事项；4=重要变化、关键分歧、明显阻塞或需要优先跟进的事项；5=紧急、重大、影响范围广或必须立即处理的决定/风险。高分只代表更重要、更需要关注，不代表内容更刺激。只依据录音原话，不得猜测或拔高。""",
    },
    "author_gossip": {
        "name": "作者自定义模板：教学与吃瓜",
        "prompt": """按教学/吃瓜关注度从 0 到 5 星评分。0=没有实质内容或无法判断；1=教学、纯工作讲解或普通闲聊；2=一般日常聊天；3=明确的人物关系、背景或动态；4=有充分依据的矛盾、争吵、个人习惯或值得关注的异常；5=有充分原话依据的重大事件。不得为了吸引眼球提高分数；传闻必须标注为传闻。""",
    },
    "meeting": {
        "name": "会议行动",
        "prompt": """按会议行动价值从 0 到 5 星评分。0=没有实质内容；1=常规同步或背景介绍；2=可供参考的进度、结论或信息；3=明确的待办、负责人、截止时间或需要跟进的决定；4=影响项目计划、资源、客户或交付的重要决策/阻塞；5=紧急升级、重大项目风险或必须立即决策的事项。只依据录音原话。""",
    },
    "risk": {
        "name": "风险与异常",
        "prompt": """按风险与异常的重要程度从 0 到 5 星评分。0=没有风险信息；1=轻微提醒或正常波动；2=需要留意但尚无明确影响的问题；3=明确的风险、投诉、冲突或依赖阻塞；4=很可能影响交付、人员、客户、合规或资金的重要风险；5=紧急安全、合规、重大损失或重大危机。传闻、不确定信息必须降级并说明依据不足。""",
    },
}
LEGACY_TEMPLATE_IDS = {"gossip": "author_gossip"}


def _load_user_templates() -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    templates: dict[str, dict[str, str]] = {}
    for template_id, item in raw.items():
        if not isinstance(template_id, str) or not template_id.startswith("user_") or not isinstance(item, dict):
            continue
        name = item.get("name")
        prompt = item.get("prompt")
        if isinstance(name, str) and name.strip() and isinstance(prompt, str) and prompt.strip():
            templates[template_id] = {"name": name.strip(), "prompt": prompt.strip()}
    return templates


def _save_user_templates(templates: dict[str, dict[str, str]]) -> None:
    TEMPLATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(templates, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=TEMPLATES_PATH.parent, suffix=".tmp") as file:
        file.write(content)
        temporary = Path(file.name)
    try:
        temporary.replace(TEMPLATES_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def rating_templates() -> dict[str, dict[str, str]]:
    return {**BUILTIN_TEMPLATES, **_load_user_templates()}


def resolve_template_id(template_id: str) -> str:
    return LEGACY_TEMPLATE_IDS.get(template_id, template_id)


def template_label(template_id: str) -> str:
    return rating_templates().get(resolve_template_id(template_id), BUILTIN_TEMPLATES["general"])["name"]


def rating_instruction(template: str, legacy_custom_prompt: str = "") -> str:
    if template == "custom" and legacy_custom_prompt.strip():
        return legacy_custom_prompt.strip()
    return rating_templates().get(resolve_template_id(template), BUILTIN_TEMPLATES["general"])["prompt"]


def save_user_template(template_id: str | None, name: str, prompt: str) -> str:
    name = name.strip()
    prompt = prompt.strip()
    if not name or not prompt:
        raise ValueError("模板名称和评分提示词不能为空。")
    templates = _load_user_templates()
    if template_id is None:
        template_id = f"user_{uuid4().hex}"
    elif template_id not in templates:
        raise ValueError("只能编辑个人创建的模板。")
    templates[template_id] = {"name": name, "prompt": prompt}
    _save_user_templates(templates)
    return template_id


def delete_user_template(template_id: str) -> None:
    templates = _load_user_templates()
    if template_id in templates:
        del templates[template_id]
        _save_user_templates(templates)
