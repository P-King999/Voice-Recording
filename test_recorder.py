import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

import numpy as np
import soundfile as sf

import recorder


class RecorderDeviceSelectionTests(TestCase):
    def test_auto_device_uses_system_default_without_probing_every_input(self) -> None:
        devices = [
            {"name": "First input", "max_input_channels": 1},
            {"name": "System default", "max_input_channels": 1},
        ]

        with (
            patch.object(recorder.sd, "query_devices", return_value=devices),
            patch.object(recorder.sd, "default", SimpleNamespace(device=[1, 2])),
            patch.object(recorder.sd, "InputStream") as input_stream,
        ):
            self.assertEqual(recorder.select_loudest_input_device(), 1)

        input_stream.assert_not_called()

    def test_auto_device_falls_back_to_first_usable_input(self) -> None:
        devices = [
            {"name": "Unavailable", "max_input_channels": 0},
            {"name": "Fallback input", "max_input_channels": 1},
        ]

        with (
            patch.object(recorder.sd, "query_devices", return_value=devices),
            patch.object(recorder.sd, "default", SimpleNamespace(device=[0, 2])),
        ):
            self.assertEqual(recorder.select_loudest_input_device(), 1)

    def test_virtual_default_input_is_skipped(self) -> None:
        devices = [
            {"name": "麦克风 (tranScreen Audio)", "max_input_channels": 2},
            {"name": "本机麦克风阵列 (Realtek High Definition Audio)", "max_input_channels": 2},
        ]

        with (
            patch.object(recorder.sd, "query_devices", return_value=devices),
            patch.object(recorder.sd, "default", SimpleNamespace(device=[0, 5])),
        ):
            self.assertEqual(recorder.select_loudest_input_device(), 1)

    def test_truncated_device_name_from_mme_still_matches(self) -> None:
        # MME cuts names at 31 characters, so .env can never match them exactly.
        devices = [
            {"name": "麦克风 (tranScreen Audio)", "max_input_channels": 2},
            {"name": "本机麦克风阵列 (Realtek High Definitio", "max_input_channels": 2},
        ]

        with patch.object(recorder.sd, "query_devices", return_value=devices):
            resolved = recorder.resolve_input_name("本机麦克风阵列 (Realtek High Definition Audio)")

        self.assertEqual(resolved, 1)

    def test_unknown_device_name_is_reported_as_missing(self) -> None:
        with patch.object(recorder.sd, "query_devices", return_value=[]):
            self.assertIsNone(recorder.resolve_input_name("不存在的设备"))


class RecorderRecoveryTests(TestCase):
    def _write_interrupted_wav(self, directory: Path, name: str = "2026-11-01_19-10-55") -> Path:
        path = directory / f"{name}.part.wav"
        with sf.SoundFile(path, mode="w", samplerate=16000, channels=1, subtype="PCM_16") as handle:
            handle.write(np.zeros(16000, dtype="float32"))
        # A process killed by shutdown leaves the header size fields at zero.
        raw = bytearray(path.read_bytes())
        raw[4:8] = b"\x00\x00\x00\x00"
        raw[40:44] = b"\x00\x00\x00\x00"
        path.write_bytes(raw)
        return path

    def test_interrupted_recording_is_repaired_and_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self._write_interrupted_wav(directory)

            with patch.object(recorder, "add_segment") as add_segment:
                recovered = recorder.repair_partial_recordings(directory)

            self.assertEqual([path.name for path in recovered], ["2026-11-01_19-10-55.wav"])
            add_segment.assert_called_once()
            repaired = directory / "2026-11-01_19-10-55.wav"
            raw = repaired.read_bytes()
            self.assertEqual(int.from_bytes(raw[4:8], "little"), len(raw) - 8)
            self.assertEqual(int.from_bytes(raw[40:44], "little"), len(raw) - 44)
            with sf.SoundFile(repaired) as handle:
                self.assertEqual(len(handle), 16000)

    def test_empty_interrupted_recording_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = self._write_interrupted_wav(directory)
            path.write_bytes(path.read_bytes()[:44])

            with patch.object(recorder, "add_segment") as add_segment:
                recovered = recorder.repair_partial_recordings(directory)

            self.assertEqual(recovered, [])
            add_segment.assert_not_called()
            self.assertFalse(path.exists())


if __name__ == "__main__":
    main()
