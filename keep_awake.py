from __future__ import annotations

import ctypes
import logging
import signal
import threading

from config import ROOT


RUNTIME_DIR = ROOT / "runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=RUNTIME_DIR / "keep_awake.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
stop_event = threading.Event()


def stop(*_: object) -> None:
    stop_event.set()


def main() -> None:
    if not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("防休眠守护程序仅支持 Windows。")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logging.info("已开始申请阻止系统自动休眠。")
    try:
        while not stop_event.is_set():
            result = kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if result == 0:
                logging.warning("Windows 拒绝了防休眠请求，错误码：%s", ctypes.get_last_error())
            stop_event.wait(30)
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        logging.info("已释放防休眠请求。")


if __name__ == "__main__":
    main()
