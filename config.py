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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_secret_file(env_name: str, filename: str) -> None:
    if os.getenv(env_name):
        return
    path = ROOT / filename
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            os.environ[env_name] = value


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
    dashscope_enable_audio_events: bool = os.getenv("DASHSCOPE_ENABLE_AUDIO_EVENTS", "true").lower() == "true"
    default_summary_mode: str = os.getenv("DEFAULT_SUMMARY_MODE", "deepseek_v4_flash").lower()
    bailian_summary_model: str = os.getenv("BAILIAN_SUMMARY_MODEL", "qwen-plus")
    llm_url: str = os.getenv("LLM_API_URL", "https://api.deepseek.com/chat/completions").rstrip("/")
    llm_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")


settings = Settings()


def ensure_storage() -> None:
    for directory in (RECORDINGS_DIR, SILENT_DIR, FAILED_DIR):
        directory.mkdir(parents=True, exist_ok=True)
