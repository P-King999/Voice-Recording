from __future__ import annotations

"""Detached helper: stop the services, run the installer silently, start again.

The dashboard cannot do this itself because stopping the agent also stops the
Streamlit process that would be running the upgrade.
"""

import ctypes
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parent
LOG = ROOT / "runtime" / "update.log"
TASK_NAME = "Voice Recorder - Start at Sign-in"


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def pid_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
    finally:
        kernel32.CloseHandle(handle)


def stop_services() -> None:
    stop_file = ROOT / "runtime" / "agent.stop"
    stop_file.touch()
    try:
        pid = int((ROOT / "runtime" / "agent.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    for _ in range(40):
        if not pid or not pid_alive(pid):
            log("services stopped")
            return
        time.sleep(1)
    log("agent still running after 40s; continuing anyway")


def start_services() -> None:
    result = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        log("started via scheduled task")
        return
    launcher = ROOT / "Launch-VoiceRecorder.vbs"
    if launcher.exists():
        subprocess.run(["wscript.exe", str(launcher)], check=False)
        log("started via launcher script")
    else:
        log(f"could not start the app: {result.stderr.strip()}")


def main() -> None:
    installer = Path(sys.argv[1])
    log(f"upgrade started: {installer.name}")
    time.sleep(3)
    stop_services()
    if not installer.exists():
        log("installer missing, aborting")
        start_services()
        return
    result = subprocess.run(
        [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/NOCANCEL"],
        check=False,
    )
    log(f"installer finished with code {result.returncode}")
    time.sleep(2)
    start_services()


if __name__ == "__main__":
    main()
