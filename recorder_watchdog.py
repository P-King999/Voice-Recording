from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

from config import ROOT


RUNTIME_DIR = ROOT / "runtime"
PID_PATH = RUNTIME_DIR / "recorder.pid"
HEARTBEAT_PATH = RUNTIME_DIR / "recorder.heartbeat"
LOG_PATH = RUNTIME_DIR / "recorder_watchdog.log"
STALE_SECONDS = 30

logging.basicConfig(filename=LOG_PATH, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def recorder_pid() -> int | None:
    try:
        value = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    return value if value > 0 else None


def start_recorder() -> None:
    process = subprocess.Popen([sys.executable, "recorder.py"], cwd=ROOT)
    PID_PATH.write_text(str(process.pid), encoding="utf-8")
    logging.info("Started recorder PID %s", process.pid)


def restart_recorder(pid: int) -> None:
    logging.error("Recorder heartbeat is stale; restarting PID %s", pid)
    if os.name == "nt":
        taskkill = os.path.join(os.environ["WINDIR"], "System32", "taskkill.exe")
        subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"], check=False)
    else:
        os.kill(pid, 15)
    PID_PATH.unlink(missing_ok=True)
    time.sleep(2)
    start_recorder()


def main() -> None:
    last_restart = 0.0
    recorder_started_at = time.monotonic()
    while True:
        pid = recorder_pid()
        if pid is None:
            start_recorder()
            recorder_started_at = time.monotonic()
        elif not HEARTBEAT_PATH.exists() or time.time() - HEARTBEAT_PATH.stat().st_mtime > STALE_SECONDS:
            now = time.monotonic()
            # A process may need a few seconds after Windows sign-in before the
            # microphone driver is ready. Do not restart it during that window.
            if (
                now - recorder_started_at >= STALE_SECONDS
                and now - last_restart >= STALE_SECONDS
            ):
                restart_recorder(pid)
                last_restart = time.monotonic()
                recorder_started_at = last_restart
        time.sleep(5)


if __name__ == "__main__":
    main()
