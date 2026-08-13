from __future__ import annotations

import os
import shutil
import sqlite3
import ctypes
from pathlib import Path

from config import DB_PATH, RECORDINGS_DIR


def _pid_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)) and exit_code.value == 259)
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def snapshot(root: Path) -> dict[str, object]:
    runtime = root / "runtime"
    data = root / "data"
    usage = shutil.disk_usage(data if data.exists() else root)
    free_gb = usage.free / 1024**3
    database_ok = False
    database_error = ""
    try:
        with sqlite3.connect(DB_PATH, timeout=3) as connection:
            connection.execute("SELECT 1")
        database_ok = True
    except (OSError, sqlite3.Error) as exc:
        database_error = str(exc)
    services = {
        name: _pid_alive(runtime / f"{name}.pid")
        for name in ("recorder", "worker", "summary_scheduler", "dashboard", "keep_awake", "power_manager", "reporter", "mail_query")
    }
    return {
        "database_ok": database_ok,
        "database_error": database_error,
        "free_gb": free_gb,
        "total_gb": usage.total / 1024**3,
        "services": services,
        "recordings_exists": RECORDINGS_DIR.exists(),
    }
