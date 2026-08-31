import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from link_studio.preview import discover_virtual_camera_devices, parse_v4l2_formats


class VirtualCameraDiscoveryTests(unittest.TestCase):
    def test_capture_formats_preserve_high_frame_rates(self):
        output = """
            Size: Discrete 1920x1080
                Interval: Discrete 0.033s (30.000 fps)
                Interval: Discrete 0.017s (60.000 fps)
            Size: Discrete 1280x720
                Interval: Discrete 0.020s (50.000 fps)
                Interval: Discrete 0.017s (59.940 fps)
        """
        self.assertEqual(
            parse_v4l2_formats(output),
            {(1920, 1080): (30, 60), (1280, 720): (50, 60)},
        )

    def test_v4l2loopback_driver_is_discovered(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "drivers/v4l2loopback"
            driver.mkdir(parents=True)
            device = root / "video20/device"
            device.mkdir(parents=True)
            (root / "video20/name").write_text("Link Studio Virtual Camera\n")
            (device / "driver").symlink_to(driver)
            self.assertEqual(discover_virtual_camera_devices(root), ["/dev/video20"])
