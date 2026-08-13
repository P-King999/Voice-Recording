from __future__ import annotations

import ctypes
import json
import logging
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from config import ROOT


RUNTIME_DIR = ROOT / "runtime"
STATE_PATH = RUNTIME_DIR / "power_state.json"
LOG_PATH = RUNTIME_DIR / "power_manager.log"
POLL_SECONDS = 30
POWER_SAVER_SCHEME = "SCHEME_MIN"

RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG_PATH, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
stop_event = threading.Event()


@dataclass(frozen=True)
class PowerStatus:
    on_ac_power: bool
    battery_percent: int


def stop(*_: object) -> None:
    stop_event.set()


def read_power_status() -> PowerStatus | None:
    """Read the Windows power source without spawning a shell process."""
    if not hasattr(ctypes, "WinDLL"):
        return None

    class SystemPowerStatus(ctypes.Structure):
        _fields_ = [("ac_line_status", ctypes.c_ubyte),
                    ("battery_flag", ctypes.c_ubyte),
                    ("battery_percent", ctypes.c_ubyte),
                    ("reserved", ctypes.c_ubyte),
                    ("life_time", ctypes.c_uint32),
                    ("full_life_time", ctypes.c_uint32)]

    status = SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        raise ctypes.WinError()
    return PowerStatus(status.ac_line_status == 1, status.battery_percent)


def run_powercfg(*args: str) -> str:
    result = subprocess.run(["powercfg.exe", *args], capture_output=True,
                            text=True, encoding="mbcs", errors="replace",
                            check=True)
    return result.stdout


def active_scheme() -> str:
    output = run_powercfg("/getactivescheme")
    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", output)
    if not match:
        raise RuntimeError(f"Could not determine active power scheme: {output}")
    return match.group(1)


def load_saved_scheme() -> str | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")).get("ac_scheme")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_scheme(guid: str) -> None:
    STATE_PATH.write_text(json.dumps({"ac_scheme": guid}), encoding="utf-8")


def clear_saved_scheme() -> None:
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def apply_power_policy(status: PowerStatus) -> None:
    current = active_scheme()
    if status.on_ac_power:
        saved = load_saved_scheme()
        if saved and current.lower() != saved.lower():
            run_powercfg("/setactive", saved)
            logging.info("AC power detected; restored power scheme %s", saved)
        clear_saved_scheme()
        return

    saved = load_saved_scheme()
    if saved and current.lower() != saved.lower():
        return
    if not saved:
        save_scheme(current)
    run_powercfg("/setactive", POWER_SAVER_SCHEME)
    logging.info("Battery power detected; enabled Windows power saver scheme")


def main() -> None:
    if not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("power_manager.py only supports Windows")
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logging.info("Power manager started")
    try:
        while not stop_event.is_set():
            try:
                status = read_power_status()
                if status:
                    apply_power_policy(status)
            except Exception:
                logging.exception("Could not update Windows power policy")
            stop_event.wait(POLL_SECONDS)
    finally:
        logging.info("Power manager stopped")


if __name__ == "__main__":
    main()
