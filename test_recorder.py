from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

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


if __name__ == "__main__":
    main()
