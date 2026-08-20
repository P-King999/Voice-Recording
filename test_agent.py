from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent


class FakeProcess:
    def __init__(self, pid: int = 100, exit_code: int | None = None) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def kill(self) -> None:
        self.killed = True
        self.exit_code = 0

    def wait(self, timeout: float | None = None) -> int:
        if self.exit_code is None:
            raise agent.subprocess.TimeoutExpired("test", timeout)
        return self.exit_code


class AgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.instance = agent.VoiceRecorderAgent(self.root)
        self.instance.runtime_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_new_recorder_is_not_stale_before_grace_period(self) -> None:
        recorder = self.instance.services["recorder"]
        recorder.started_at = 100
        with patch.object(agent.time, "monotonic", return_value=100 + agent.RECORDER_STALE_SECONDS - 1):
            self.assertFalse(self.instance.service_is_stale(recorder))

    def test_service_specs_keep_runtime_state_under_agent_root(self) -> None:
        self.assertEqual(self.instance.services["recorder"].spec.heartbeat_path, self.root / "runtime" / "recorder.heartbeat")

    def test_crashed_service_uses_backoff_before_restart(self) -> None:
        worker = self.instance.services["worker"]
        self.instance.services = {"worker": worker}
        worker.process = FakeProcess(exit_code=1)  # type: ignore[assignment]
        with patch.object(agent.time, "monotonic", return_value=100):
            self.instance.supervise_once()
        self.assertIsNone(worker.process)
        self.assertEqual(worker.next_start_at, 101)
        self.assertEqual(len(worker.restart_times), 1)

    def test_stale_recorder_requests_file_based_graceful_stop(self) -> None:
        recorder = self.instance.services["recorder"]
        self.instance.services = {"recorder": recorder}
        recorder.process = FakeProcess()  # type: ignore[assignment]
        recorder.started_at = 1
        with (
            patch.object(agent.time, "monotonic", side_effect=[100, 100, 100]),
            patch.object(agent.time, "time", return_value=100),
            patch.object(self.instance, "stop_service") as stop_service,
        ):
            self.instance.supervise_once()
        stop_service.assert_called_once_with(recorder, timeout=agent.RECORDER_STOP_TIMEOUT_SECONDS)
        self.assertIsNone(recorder.process)

    def test_paused_recorder_is_stopped_without_being_scheduled_for_restart(self) -> None:
        recorder = self.instance.services["recorder"]
        self.instance.services = {"recorder": recorder}
        recorder.process = FakeProcess()  # type: ignore[assignment]
        (self.instance.runtime_dir / "recorder.paused").touch()

        with patch.object(self.instance, "stop_service") as stop_service:
            self.instance.supervise_once()

        stop_service.assert_called_once_with(recorder, timeout=agent.RECORDER_STOP_TIMEOUT_SECONDS)
        self.assertIsNone(recorder.process)
        self.assertEqual(recorder.next_start_at, 0.0)

    def test_dashboard_restart_request_stops_only_the_dashboard(self) -> None:
        dashboard = self.instance.services["dashboard"]
        dashboard.process = FakeProcess()  # type: ignore[assignment]
        (self.instance.runtime_dir / "dashboard.restart").touch()

        with patch.object(self.instance, "stop_service") as stop_service:
            self.instance.handle_dashboard_restart_request()

        stop_service.assert_called_once_with(dashboard)
        self.assertIsNone(dashboard.process)
        self.assertFalse((self.instance.runtime_dir / "dashboard.restart").exists())

    def test_dashboard_start_reads_the_current_bind_address_from_env(self) -> None:
        (self.root / ".env").write_text("DASHBOARD_BIND_ADDRESS=0.0.0.0\n", encoding="utf-8")
        dashboard = self.instance.services["dashboard"]

        with patch.object(agent.subprocess, "Popen", return_value=FakeProcess()) as popen:
            self.instance.start_service(dashboard)

        command = popen.call_args.args[0]
        self.assertIn("--server.address=0.0.0.0", command)


if __name__ == "__main__":
    unittest.main()
