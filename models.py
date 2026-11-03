from __future__ import annotations

"""Live model catalog.

DeepSeek renamed its models (deepseek-v4-flash -> deepseek-flash / V4.1-Flash),
so model IDs are read from the provider instead of being hard-coded. The result
is cached for half a day, and every caller falls back to the bundled defaults
when the API is unreachable.
"""

import json
import logging
import time
from pathlib import Path

import requests

from config import ROOT, settings

CACHE_PATH = ROOT / "runtime" / "models-cache.json"
CACHE_TTL_SECONDS = 12 * 3600
FAILURE_BACKOFF_SECONDS = 10 * 60
REQUEST_TIMEOUT_SECONDS = 15

FALLBACK_DEEPSEEK_MODELS: tuple[dict[str, str], ...] = (
    {"id": "deepseek-flash", "name": "DeepSeek-V4.1-Flash"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro"},
)
FALLBACK_BAILIAN_MODELS: tuple[dict[str, str], ...] = (
    {"id": "qwen-plus", "name": "千问 Qwen Plus"},
)
# Older .env files and saved settings still use the previous key names.
LEGACY_KEYS = {
    "deepseek_v4_flash": "deepseek_flash",
    "deepseek_v4_pro": "deepseek_v4_pro",
}


def mode_key(model_id: str) -> str:
    return model_id.replace("-", "_").replace(".", "_").replace(":", "_")


def _api_base() -> str:
    url = (settings.llm_url or "https://api.deepseek.com").rstrip("/")
    for suffix in ("/chat/completions",):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _read_cache() -> dict[str, object]:
    try:
        loaded = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache(cache: dict[str, object]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(CACHE_PATH)


def fetch_deepseek_models(timeout: float = REQUEST_TIMEOUT_SECONDS) -> list[dict[str, str]]:
    if not settings.llm_key:
        raise RuntimeError("未配置 DeepSeek API Key。")
    response = requests.get(
        f"{_api_base()}/models",
        headers={"Authorization": f"Bearer {settings.llm_key}"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json().get("data") or []
    models = [
        {"id": str(item["id"]), "name": str(item.get("name") or item["id"])}
        for item in payload
        if isinstance(item, dict) and item.get("id")
    ]
    if not models:
        raise RuntimeError("DeepSeek 没有返回任何可用模型。")
    return models


def deepseek_models(force: bool = False) -> list[dict[str, str]]:
    """Cached provider catalog with a back-off so an offline laptop stays responsive."""
    cache = _read_cache()
    entry = cache.get("deepseek") if isinstance(cache.get("deepseek"), dict) else {}
    fetched_at = float(entry.get("fetched_at") or 0)
    attempted_at = float(entry.get("attempted_at") or 0)
    cached = [item for item in entry.get("models") or [] if isinstance(item, dict)]
    now = time.time()
    if not force and cached and (
        now - fetched_at < CACHE_TTL_SECONDS or now - attempted_at < FAILURE_BACKOFF_SECONDS
    ):
        return cached
    try:
        models = fetch_deepseek_models()
    except Exception:
        logging.warning("Could not refresh the DeepSeek model list; using cache/defaults", exc_info=True)
        cache["deepseek"] = {**entry, "attempted_at": now}
        try:
            _write_cache(cache)
        except OSError:
            pass
        return cached or [dict(item) for item in FALLBACK_DEEPSEEK_MODELS]
    cache["deepseek"] = {"fetched_at": now, "attempted_at": now, "models": models}
    try:
        _write_cache(cache)
    except OSError:
        logging.exception("Could not cache the model list")
    return models


def refresh(force: bool = True) -> dict[str, list[dict[str, str]]]:
    """Fetch the provider catalog now (used by the dashboard "刷新模型" button)."""
    return {"deepseek": deepseek_models(force=force)}


def last_refreshed() -> float:
    cache = _read_cache()
    entry = cache.get("deepseek")
    return float(entry.get("fetched_at") or 0) if isinstance(entry, dict) else 0.0


def summary_modes() -> dict[str, tuple[str, str, str]]:
    """Map mode key -> (display label, provider, API model id)."""
    modes: dict[str, tuple[str, str, str]] = {}
    for item in deepseek_models():
        modes[mode_key(item["id"])] = (item["name"], "deepseek", item["id"])
    for item in FALLBACK_BAILIAN_MODELS:
        modes.setdefault(mode_key(item["id"]), (item["name"], "bailian", item["id"]))
    return modes


def summary_mode_labels() -> dict[str, str]:
    return {key: value[0] for key, value in summary_modes().items()}


def resolve_key(mode: str | None) -> str:
    return LEGACY_KEYS.get(mode or "", mode or "")


def resolve(mode: str | None) -> tuple[str, str, str] | None:
    modes = summary_modes()
    key = resolve_key(mode)
    return modes.get(key) or modes.get(mode or "")


def default_mode() -> str:
    modes = summary_modes()
    configured = resolve_key(settings.default_summary_mode)
    if configured in modes:
        return configured
    return next(iter(modes), "qwen_plus")


def fallback_modes(mode: str, limit: int = 2) -> list[str]:
    return [key for key in summary_modes() if key != mode][:limit]


def active_chat_model() -> str:
    """The DeepSeek chat model used for conversation analysis.

    A configured ID that the provider no longer offers is replaced by the first
    model the catalog reports, so a rename cannot silently break analysis.
    """
    models = deepseek_models()
    available = [item["id"] for item in models]
    configured = settings.llm_model
    return configured if configured in available else (available[0] if available else configured)
