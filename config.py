from __future__ import annotations

import csv
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
SILENT_DIR = DATA_DIR / "silent"
FAILED_DIR = DATA_DIR / "failed"
DB_PATH = DATA_DIR / "voice_recorder.sqlite3"
_ENV_WRITE_LOCK = threading.Lock()


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip('"').strip("'")
        # An empty inherited environment variable should not mask a value from .env.
        if not os.environ.get(normalized_key):
            os.environ[normalized_key] = normalized_value


def load_secret_file(env_name: str, filename: str) -> None:
    if os.getenv(env_name):
        return
    path = ROOT / filename
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            os.environ[env_name] = value


def _persist_env_document(env_path: Path, content: str) -> None:
    """Write .env safely on Windows, where replacing a briefly-open file can fail."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_fd, temporary_name = tempfile.mkstemp(prefix=f".{env_path.name}.", suffix=".tmp", dir=env_path.parent)
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(content, encoding="utf-8")
        with _ENV_WRITE_LOCK:
            for attempt in range(8):
                try:
                    temporary.replace(env_path)
                    return
                except PermissionError:
                    if attempt == 7:
                        # Some Windows file locks deny rename/delete but still allow writing in place.
                        env_path.write_text(content, encoding="utf-8")
                        return
                    time.sleep(0.15)
    finally:
        temporary.unlink(missing_ok=True)


def save_env_value(key: str, value: str) -> None:
    """Persist a user-controlled setting while preserving comments in .env."""
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    prefix = f"{key}="
    replacement = f"{prefix}{value}"
    for index, line in enumerate(lines):
        if line.strip().startswith(prefix):
            lines[index] = replacement
            break
    else:
        lines.extend(["", replacement])
    _persist_env_document(env_path, "\n".join(lines) + "\n")


def read_env_document() -> str:
    """Return the editable local configuration document without loading secrets elsewhere."""
    env_path = ROOT / ".env"
    return env_path.read_text(encoding="utf-8") if env_path.exists() else ""


def save_env_document(content: str) -> None:
    """Replace .env only after a small validation that catches accidental malformed lines."""
    if "\x00" in content:
        raise ValueError("配置中不能包含空字符。")
    for line_number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" not in stripped:
            raise ValueError(f"第 {line_number} 行缺少 = 号。")
    env_path = ROOT / ".env"
    _persist_env_document(env_path, content.rstrip() + "\n")


def dashboard_access_mode(password: str, bind_address: str, local_no_password: bool) -> str:
    """Return the dashboard access flow for the current local configuration."""
    if not password.strip():
        return "setup"
    if bind_address.strip() == "127.0.0.1" and local_no_password:
        return "unprotected"
    return "login"


load_dotenv()


def configured_recordings_dir() -> Path:
    """Resolve the user-selected recording directory from the local configuration."""
    configured = os.getenv("RECORDINGS_DIR", "").strip()
    if not configured:
        return DATA_DIR / "recordings"
    path = Path(configured).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


# Reassign after .env is loaded so every importing service uses the selected folder.
RECORDINGS_DIR = configured_recordings_dir()
load_secret_file("LLM_API_KEY", "deepseekkey.txt")
load_secret_file("DASHSCOPE_API_KEY", "dashscopekey.txt")
load_secret_file("DASHSCOPE_API_KEY", "bailiankey.txt")


def load_bailian_api_key_csv() -> None:
    if os.getenv("DASHSCOPE_API_KEY"):
        return
    for path in ROOT.glob("*.csv"):
        if "apikey" not in path.name.lower():
            continue
        with path.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.reader(file))
        values = {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2 and row[0].strip()}
        api_key = values.get("apiKey", "")
        if api_key:
            os.environ["DASHSCOPE_API_KEY"] = api_key
            if values.get("workspaceId"):
                os.environ.setdefault("DASHSCOPE_WORKSPACE", values["workspaceId"])
            return


load_bailian_api_key_csv()


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    sample_rate: int = env_int("SAMPLE_RATE", 16000)
    channels: int = env_int("CHANNELS", 1)
    segment_seconds: int = env_int("SEGMENT_SECONDS", 3600)
    device: str | None = os.getenv("RECORDING_DEVICE") or None
    silence_threshold_dbfs: float = float(os.getenv("SILENCE_THRESHOLD_DBFS", "-48"))
    min_active_seconds: float = float(os.getenv("MIN_ACTIVE_SECONDS", "3"))
    stt_provider: str = os.getenv("STT_PROVIDER", "dashscope_filetrans").lower()
    dashscope_api_key: str = os.getenv("DASHSCOPE_API_KEY", "")
    dashscope_workspace: str = os.getenv("DASHSCOPE_WORKSPACE", "")
    dashscope_asr_model: str = os.getenv("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash-filetrans")
    dashscope_delete_uploaded_file: bool = os.getenv("DASHSCOPE_DELETE_UPLOADED_FILE", "true").lower() == "true"
    dashscope_enable_diarization: bool = os.getenv("DASHSCOPE_ENABLE_DIARIZATION", "true").lower() == "true"
    default_summary_mode: str = os.getenv("DEFAULT_SUMMARY_MODE", "deepseek_v4_flash").lower()
    rating_template: str = os.getenv("RATING_TEMPLATE", "general").lower()
    rating_custom_prompt: str = os.getenv("RATING_CUSTOM_PROMPT", "")
    bailian_summary_model: str = os.getenv("BAILIAN_SUMMARY_MODEL", "qwen-plus")
    llm_url: str = os.getenv("LLM_API_URL", "https://api.deepseek.com/chat/completions").rstrip("/")
    llm_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "")
    dashboard_bind_address: str = os.getenv("DASHBOARD_BIND_ADDRESS", "127.0.0.1").strip() or "127.0.0.1"
    dashboard_local_no_password: bool = os.getenv("DASHBOARD_LOCAL_NO_PASSWORD", "false").lower() == "true"
    disk_warning_gb: float = float(os.getenv("DISK_WARNING_GB", "10"))
    report_enabled: bool = os.getenv("REPORT_ENABLED", "false").lower() == "true"
    report_times: tuple[str, ...] = tuple(
        value.strip() for value in os.getenv("REPORT_TIMES", "12:10,17:10,21:10").split(",") if value.strip()
    )
    report_channels: tuple[str, ...] = tuple(
        value.strip().lower() for value in os.getenv("REPORT_CHANNELS", "smtp").split(",") if value.strip()
    )
    report_email_recipient: str = os.getenv("REPORT_EMAIL_RECIPIENT", "").strip()
    report_smtp_host: str = os.getenv("REPORT_SMTP_HOST", "smtp.qq.com").strip()
    report_smtp_port: int = env_int("REPORT_SMTP_PORT", 465)
    report_smtp_username: str = os.getenv("REPORT_SMTP_USERNAME", "").strip()
    report_smtp_auth_code: str = os.getenv("REPORT_SMTP_AUTH_CODE", "").strip()
    report_wecom_webhook: str = os.getenv("REPORT_WECOM_WEBHOOK", "").strip()
    report_pushplus_token: str = os.getenv("REPORT_PUSHPLUS_TOKEN", "").strip()
    feishu_app_id: str = os.getenv("FEISHU_APP_ID", "").strip()
    feishu_app_secret: str = os.getenv("FEISHU_APP_SECRET", "").strip()
    feishu_chat_name: str = os.getenv("FEISHU_CHAT_NAME", "").strip()
    feishu_bitable_app_token: str = os.getenv("FEISHU_BITABLE_APP_TOKEN", "").strip()
    feishu_bitable_table_id: str = os.getenv("FEISHU_BITABLE_TABLE_ID", "").strip()
    report_query_enabled: bool = os.getenv("REPORT_QUERY_ENABLED", "true").lower() == "true"
    report_imap_host: str = os.getenv("REPORT_IMAP_HOST", "imap.qq.com").strip()
    report_imap_port: int = env_int("REPORT_IMAP_PORT", 993)
    report_query_interval_seconds: int = env_int("REPORT_QUERY_INTERVAL_SECONDS", 60)
    summary_scheduler_interval_seconds: int = env_int("SUMMARY_SCHEDULER_INTERVAL_SECONDS", 60)
    summary_lookback_days: int = env_int("SUMMARY_LOOKBACK_DAYS", 7)
    summary_finalize_delay_seconds: int = env_int("SUMMARY_FINALIZE_DELAY_SECONDS", 30)


settings = Settings()


def ensure_storage() -> None:
    for directory in (RECORDINGS_DIR, SILENT_DIR, FAILED_DIR):
        directory.mkdir(parents=True, exist_ok=True)
