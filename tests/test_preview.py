import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gi.repository import Gst

from link_studio.preview import (
    Frame,
    PreviewStream,
    Recorder,
    VirtualCameraPublisher,
    _gst_quote,
    discover_virtual_camera_devices,
    parse_v4l2_formats,
)


class _FakeSource:
    def __init__(self):
        self.buffers = []

    def emit(self, signal, buffer):
        self.buffers.append((signal, buffer))
        return Gst.FlowReturn.OK


class _FakeEffects:
    def __init__(self):
        self.properties = {}

    def set_property(self, name, value):
        self.properties[name] = value


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

    def test_gstreamer_values_are_quoted(self):
        self.assertEqual(_gst_quote('/dev/video" odd\\name'), '/dev/video\\" odd\\\\name')

    def test_recorded_frames_use_pipeline_clock_timestamps(self):
        recorder = Recorder(Path("recording.mp4"), 2, 1, 30)
        source = _FakeSource()
        recorder.source = source
        frame = Frame(2, 1, 6, b"abcdef", 12 * Gst.SECOND)

        recorder.push(frame)

        buffer = source.buffers[0][1]
        self.assertEqual(buffer.pts, Gst.CLOCK_TIME_NONE)
        self.assertEqual(buffer.duration, Gst.SECOND // 30)
        self.assertEqual(buffer.extract_dup(0, buffer.get_size()), frame.data)

    def test_virtual_camera_frames_also_use_live_timestamps(self):
        publisher = VirtualCameraPublisher("/dev/video20", 2, 1, 30)
        source = _FakeSource()
        publisher.source = source

        publisher.push(Frame(2, 1, 6, b"abcdef", 0))

        self.assertEqual(source.buffers[0][1].pts, Gst.CLOCK_TIME_NONE)

    def test_live_filter_is_reapplied_to_each_new_pipeline_element(self):
        preview = PreviewStream("/dev/video0")
        preview.set_filter("punch")
        first = _FakeEffects()
        second = _FakeEffects()

        preview._bind_effects(first)
        preview._bind_effects(second)

        expected = {
            "brightness": 0.0,
            "saturation": 1.22,
            "contrast": 1.14,
            "hue": 1.0,
        }
        self.assertEqual(preview.filter_name, "punch")
        self.assertEqual(first.properties, expected)
        self.assertEqual(second.properties, expected)
