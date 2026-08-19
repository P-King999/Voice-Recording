from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config import ROOT, settings


RUNTIME_DIR = ROOT / "runtime"
STOP_REQUEST_PATH = RUNTIME_DIR / "agent.stop"
RECORDER_STOP_REQUEST_PATH = RUNTIME_DIR / "recorder.stop"
POLL_SECONDS = 2
RECORDER_STALE_SECONDS = 30
RECORDER_STOP_TIMEOUT_SECONDS = 20
RESTART_WINDOW_SECONDS = 600
MAX_RESTARTS_IN_WINDOW = 5
MAX_RESTART_DELAY_SECONDS = 60


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    arguments: tuple[str, ...]
    heartbeat_path: Path | None = None


@dataclass
class ManagedService:
    spec: ServiceSpec
    process: subprocess.Popen[bytes] | None = None
    restart_times: deque[float] = field(default_factory=deque)
    started_at: float = 0.0
    next_start_at: float = 0.0
    disabled: bool = False


def service_specs(runtime_dir: Path = RUNTIME_DIR) -> tuple[ServiceSpec, ...]:
    return (
        ServiceSpec("recorder", ("recorder.py",), runtime_dir / "recorder.heartbeat"),
        ServiceSpec("worker", ("worker.py",)),
        ServiceSpec("summary_scheduler", ("summary_scheduler.py",)),
        ServiceSpec("keep_awake", ("keep_awake.py",)),
        ServiceSpec("power_manager", ("power_manager.py",)),
        ServiceSpec(
            "dashboard",
            dashboard_arguments(settings.dashboard_bind_address),
        ),
        ServiceSpec("reporter", ("reporter.py",)),
        ServiceSpec("mail_query", ("mail_query.py",)),
    )


def dashboard_arguments(bind_address: str) -> tuple[str, ...]:
    return (
        "-m",
        "streamlit",
        "run",
        "dashboard.py",
        "--server.headless=true",
        f"--server.address={bind_address}",
        "--server.port=8501",
        "--browser.gatherUsageStats=false",
    )


def configured_dashboard_bind_address(root: Path) -> str:
    """Read the current bind address so a dashboard-only restart uses saved settings."""
    env_path = root / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "DASHBOARD_BIND_ADDRESS":
                return value.strip().strip('"').strip("'") or "127.0.0.1"
    return settings.dashboard_bind_address


class VoiceRecorderAgent:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.runtime_dir = root / "runtime"
        self.stop_requested = False
        self.mutex_handle: int | None = None
        self.job_handle: int | None = None
        self.services = {spec.name: ManagedService(spec) for spec in service_specs(self.runtime_dir)}

    def pid_path(self, name: str) -> Path:
        return self.runtime_dir / f"{name}.pid"

    def request_stop(self, *_: object) -> None:
        self.stop_requested = True

    def acquire_single_instance_lock(self) -> bool:
        if os.name != "nt":
            return True
        mutex_name = f"Local\\VoiceRecorderAgent-{sha256(str(self.root).encode()).hexdigest()[:16]}"
        import ctypes

        ctypes.set_last_error(0)
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create Agent mutex")
        if ctypes.get_last_error() == 183:
            ctypes.windll.kernel32.CloseHandle(handle)
            logging.info("Another Voice Recorder Agent is already running")
            return False
        self.mutex_handle = int(handle)
        return True

    def release_single_instance_lock(self) -> None:
        if self.mutex_handle is not None and os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.mutex_handle)
        self.mutex_handle = None

    def create_process_job(self) -> None:
        if os.name != "nt":
            return
        import ctypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("read_operation_count", "write_operation_count", "other_operation_count", "read_transfer_count", "write_transfer_count", "other_transfer_count")]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time_limit", ctypes.c_int64),
                ("per_job_user_time_limit", ctypes.c_int64),
                ("limit_flags", ctypes.c_uint32),
                ("minimum_working_set_size", ctypes.c_size_t),
                ("maximum_working_set_size", ctypes.c_size_t),
                ("active_process_limit", ctypes.c_uint32),
                ("affinity", ctypes.c_size_t),
                ("priority_class", ctypes.c_uint32),
                ("scheduling_class", ctypes.c_uint32),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [("basic_limit_information", BasicLimitInformation), ("io_info", IoCounters), ("process_memory_limit", ctypes.c_size_t), ("job_memory_limit", ctypes.c_size_t), ("peak_process_memory_used", ctypes.c_size_t), ("peak_job_memory_used", ctypes.c_size_t)]

        handle = ctypes.windll.kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create Agent process job")
        info = ExtendedLimitInformation()
        info.basic_limit_information.limit_flags = 0x00002000
        if not ctypes.windll.kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            ctypes.windll.kernel32.CloseHandle(handle)
            raise OSError(error, "Could not configure Agent process job")
        self.job_handle = int(handle)

    def close_process_job(self) -> None:
        if self.job_handle is not None and os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.job_handle)
        self.job_handle = None

    def start_service(self, service: ManagedService) -> None:
        service.spec.heartbeat_path and service.spec.heartbeat_path.unlink(missing_ok=True)
        arguments = (
            dashboard_arguments(configured_dashboard_bind_address(self.root))
            if service.spec.name == "dashboard"
            else service.spec.arguments
        )
        command = [sys.executable, *arguments]
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        process = subprocess.Popen(command, cwd=self.root, creationflags=creation_flags)
        if self.job_handle is not None and os.name == "nt":
            import ctypes

            process_handle = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x0100, False, process.pid)
            if not process_handle:
                process.terminate()
                raise OSError(ctypes.get_last_error(), f"Could not open {service.spec.name} process")
            try:
                if not ctypes.windll.kernel32.AssignProcessToJobObject(self.job_handle, process_handle):
                    process.terminate()
                    raise OSError(ctypes.get_last_error(), f"Could not manage {service.spec.name} process")
            finally:
                ctypes.windll.kernel32.CloseHandle(process_handle)
        service.process = process
        service.started_at = time.monotonic()
        self.pid_path(service.spec.name).write_text(str(process.pid), encoding="utf-8")
        logging.info("Started %s with PID %s", service.spec.name, process.pid)

    def service_is_stale(self, service: ManagedService) -> bool:
        heartbeat = service.spec.heartbeat_path
        if heartbeat is None or time.monotonic() - service.started_at < RECORDER_STALE_SECONDS:
            return False
        return not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > RECORDER_STALE_SECONDS

    def schedule_restart(self, service: ManagedService, reason: str) -> None:
        now = time.monotonic()
        while service.restart_times and now - service.restart_times[0] > RESTART_WINDOW_SECONDS:
            service.restart_times.popleft()
        service.restart_times.append(now)
        if len(service.restart_times) > MAX_RESTARTS_IN_WINDOW:
            service.disabled = True
            logging.critical("Disabled %s after repeated failures: %s", service.spec.name, reason)
            return
        delay = min(2 ** (len(service.restart_times) - 1), MAX_RESTART_DELAY_SECONDS)
        service.next_start_at = now + delay
        logging.warning("Restarting %s in %ss: %s", service.spec.name, delay, reason)

    def stop_service(self, service: ManagedService, timeout: float = 5) -> None:
        process = service.process
        if process is None or process.poll() is not None:
            return
        if service.spec.name == "recorder":
            RECORDER_STOP_REQUEST_PATH.touch()
        else:
            process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logging.error("%s did not stop within %ss; forcing termination", service.spec.name, timeout)
            if os.name == "nt":
                taskkill = Path(os.environ["WINDIR"]) / "System32" / "taskkill.exe"
                subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"], check=False)
            else:
                process.kill()
            process.wait(timeout=5)

    def handle_dashboard_restart_request(self) -> None:
        request_path = self.runtime_dir / "dashboard.restart"
        if not request_path.exists():
            return
        request_path.unlink(missing_ok=True)
        dashboard = self.services.get("dashboard")
        if dashboard is None or dashboard.disabled:
            return
        logging.info("Restarting dashboard to apply updated settings")
        self.stop_service(dashboard)
        dashboard.process = None
        dashboard.started_at = 0.0
        self.pid_path("dashboard").unlink(missing_ok=True)
        dashboard.next_start_at = 0.0

    def supervise_once(self) -> None:
        self.handle_dashboard_restart_request()
        now = time.monotonic()
        for service in self.services.values():
            process = service.process
            if service.disabled:
                continue
            if process is None:
                if now >= service.next_start_at:
                    self.start_service(service)
                continue
            exit_code = process.poll()
            if exit_code is not None:
                logging.error("%s exited with code %s", service.spec.name, exit_code)
                service.process = None
                service.started_at = 0.0
                self.pid_path(service.spec.name).unlink(missing_ok=True)
                self.schedule_restart(service, f"exit code {exit_code}")
                continue
            if self.service_is_stale(service):
                logging.error("%s heartbeat is stale; requesting a graceful stop", service.spec.name)
                self.stop_service(service, timeout=RECORDER_STOP_TIMEOUT_SECONDS)
                service.process = None
                service.started_at = 0.0
                self.pid_path(service.spec.name).unlink(missing_ok=True)
                self.schedule_restart(service, "stale heartbeat")

    def run(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if not self.acquire_single_instance_lock():
            return
        self.create_process_job()
        STOP_REQUEST_PATH.unlink(missing_ok=True)
        RECORDER_STOP_REQUEST_PATH.unlink(missing_ok=True)
        (self.runtime_dir / "dashboard.restart").unlink(missing_ok=True)
        self.pid_path("agent").write_text(str(os.getpid()), encoding="utf-8")
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        logging.info("Voice Recorder Agent started")
        try:
            while not self.stop_requested:
                if STOP_REQUEST_PATH.exists():
                    logging.info("Received stop request")
                    self.stop_requested = True
                    continue
                self.supervise_once()
                time.sleep(POLL_SECONDS)
        finally:
            logging.info("Voice Recorder Agent stopping")
            for service in self.services.values():
                self.stop_service(service, timeout=RECORDER_STOP_TIMEOUT_SECONDS if service.spec.name == "recorder" else 5)
                self.pid_path(service.spec.name).unlink(missing_ok=True)
            STOP_REQUEST_PATH.unlink(missing_ok=True)
            RECORDER_STOP_REQUEST_PATH.unlink(missing_ok=True)
            self.pid_path("agent").unlink(missing_ok=True)
            self.close_process_job()
            self.release_single_instance_lock()


def configure_logging() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(RUNTIME_DIR / "agent.log", when="midnight", backupCount=14, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> None:
    configure_logging()
    VoiceRecorderAgent().run()


if __name__ == "__main__":
    main()
