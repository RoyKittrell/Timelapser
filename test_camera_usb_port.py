import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import camera_usb_port
import mega4_usb_import


class CameraUsbPortTests(unittest.TestCase):
    def test_discovers_olympus_hub_and_port(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            device = root / "1-1.3"
            device.mkdir()
            (device / "idVendor").write_text("07B4\n")
            (device / "idProduct").write_text("012E\n")
            self.assertEqual(
                camera_usb_port.discover_camera(root),
                {"hub": "1-1", "port": 3, "usb_path": "1-1.3",
                 "vendor": "07b4", "product": "012e"},
            )

    def test_no_camera_returns_none(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(camera_usb_port.discover_camera(Path(temp)))

    def test_saved_mapping_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "camera.json"
            mapping = {"hub": "1-1", "port": 3, "usb_path": "1-1.3"}
            camera_usb_port.save_mapping(mapping, state)
            self.assertEqual(camera_usb_port.load_mapping(state), mapping)

    @patch("mega4_usb_import.subprocess.run")
    def test_camera_partition_follows_model_not_device_letter(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({"blockdevices": [
                {"path": "/dev/sda", "model": "My Passport", "type": "disk",
                 "children": [{"path": "/dev/sda1", "type": "part"}]},
                {"path": "/dev/sdb", "model": "E-M5MarkIII", "type": "disk",
                 "children": [{"path": "/dev/sdb1", "type": "part"}]},
            ]}),
        )
        self.assertEqual(mega4_usb_import.camera_partition(), Path("/dev/sdb1"))

    @patch("mega4_usb_import.subprocess.run")
    def test_camera_partition_waits_for_partition_table(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({"blockdevices": [
                {"path": "/dev/sdc", "model": "E-M5MarkIII", "type": "disk"},
            ]}),
        )
        self.assertIsNone(mega4_usb_import.camera_partition())

    @patch("mega4_usb_import.subprocess.run")
    def test_camera_partition_supports_flat_lsblk_output(self, run):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({"blockdevices": [
                {"name": "sda", "path": "/dev/sda", "model": "E-M5MarkIII", "type": "disk", "pkname": None},
                {"name": "sda1", "path": "/dev/sda1", "model": None, "type": "part", "pkname": "sda"},
            ]}),
        )
        self.assertEqual(mega4_usb_import.camera_partition(), Path("/dev/sda1"))


if __name__ == "__main__":
    unittest.main()
