from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
SILENT_DIR = DATA_DIR / "silent"
FAILED_DIR = DATA_DIR / "failed"
DB_PATH = DATA_DIR / "voice_recorder.sqlite3"


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
    temporary = env_path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(env_path)


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
    temporary = env_path.with_suffix(".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    temporary.replace(env_path)


load_dotenv()
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
    bailian_summary_model: str = os.getenv("BAILIAN_SUMMARY_MODEL", "qwen-plus")
    llm_url: str = os.getenv("LLM_API_URL", "https://api.deepseek.com/chat/completions").rstrip("/")
    llm_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    dashboard_password: str = os.getenv("DASHBOARD_PASSWORD", "")
    disk_warning_gb: float = float(os.getenv("DISK_WARNING_GB", "10"))
    report_enabled: bool = os.getenv("REPORT_ENABLED", "false").lower() == "true"
    report_times: tuple[str, ...] = tuple(
        value.strip() for value in os.getenv("REPORT_TIMES", "12:10,18:10").split(",") if value.strip()
    )
    report_channels: tuple[str, ...] = tuple(
        value.strip().lower() for value in os.getenv("REPORT_CHANNELS", "smtp").split(",") if value.strip()
    )
    report_email_recipient: str = os.getenv("REPORT_EMAIL_RECIPIENT", "sp.99@qq.com").strip()
    report_smtp_host: str = os.getenv("REPORT_SMTP_HOST", "smtp.qq.com").strip()
    report_smtp_port: int = env_int("REPORT_SMTP_PORT", 465)
    report_smtp_username: str = os.getenv("REPORT_SMTP_USERNAME", "").strip()
    report_smtp_auth_code: str = os.getenv("REPORT_SMTP_AUTH_CODE", "").strip()
    report_wecom_webhook: str = os.getenv("REPORT_WECOM_WEBHOOK", "").strip()
    report_pushplus_token: str = os.getenv("REPORT_PUSHPLUS_TOKEN", "").strip()
    report_query_enabled: bool = os.getenv("REPORT_QUERY_ENABLED", "true").lower() == "true"
    report_imap_host: str = os.getenv("REPORT_IMAP_HOST", "imap.qq.com").strip()
    report_imap_port: int = env_int("REPORT_IMAP_PORT", 993)
    report_query_interval_seconds: int = env_int("REPORT_QUERY_INTERVAL_SECONDS", 60)


settings = Settings()


def ensure_storage() -> None:
    for directory in (RECORDINGS_DIR, SILENT_DIR, FAILED_DIR):
        directory.mkdir(parents=True, exist_ok=True)
