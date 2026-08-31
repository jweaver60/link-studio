import ctypes
import struct
import threading
import unittest

from link_studio.camera import (
    UVCIOC_CTRL_QUERY,
    VIDIOC_G_CTRL,
    VIDIOC_QUERYCAP,
    VIDIOC_S_CTRL,
    Camera,
    CameraError,
    CameraOperationCancelled,
    _UvcXuControlQuery,
)
from link_studio.constants import AUDIO_MODES, STANDARD_CONTROLS, VIDEO_MODES


class CameraAbiTests(unittest.TestCase):
    def test_linux_ioctl_layout_matches_uvc_abi(self):
        self.assertEqual(ctypes.sizeof(_UvcXuControlQuery), 16)
        self.assertEqual(UVCIOC_CTRL_QUERY, 0xC0107521)
        self.assertEqual(VIDIOC_QUERYCAP, 0x80685600)
        self.assertEqual(VIDIOC_G_CTRL, 0xC008561B)
        self.assertEqual(VIDIOC_S_CTRL, 0xC008561C)

    def test_link2_control_ranges_are_sane(self):
        self.assertEqual(STANDARD_CONTROLS["zoom"].minimum, 100)
        self.assertEqual(STANDARD_CONTROLS["zoom"].maximum, 400)
        self.assertLess(STANDARD_CONTROLS["pan"].minimum, 0)
        self.assertGreater(STANDARD_CONTROLS["pan"].maximum, 0)
        self.assertEqual(STANDARD_CONTROLS["white_balance_temperature"].maximum, 10000)

    def test_link2_ai_mode_ids_are_unique(self):
        ids = [wire[0] for wire in VIDEO_MODES.values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(VIDEO_MODES["tracking"][:2], (0x01, 0x00))
        self.assertEqual(VIDEO_MODES["deskview"][:2], (0x06, 0x10))

    def test_device_information_is_decoded_without_writes(self):
        camera = object.__new__(Camera)
        raw = bytearray(234)
        encoded = b"17B0582397030726780200\0v3.2.6.8_build5\0Insta Puc2_flow\0"
        raw[: len(encoded)] = encoded
        camera.xu_get = lambda *_args: bytes(raw)

        self.assertEqual(
            camera.get_device_information(),
            {
                "firmware_version": "v3.2.6.8_build5",
                "hardware_revision": "17B0582397030726780200",
                "firmware_build": "Insta Puc2_flow",
            },
        )

    def test_link2_audio_modes_use_the_noise_cancel_selector(self):
        self.assertEqual(
            AUDIO_MODES,
            {"music_balance": 0, "voice_focus": 1, "voice_suppression": 2},
        )
        camera = object.__new__(Camera)
        writes = []
        camera.xu_set = lambda unit, selector, payload: writes.append((unit, selector, payload))
        camera.xu_get = lambda *_args: bytes([2])

        self.assertEqual(camera.set_audio_mode("voice_suppression"), "voice_suppression")
        self.assertEqual(writes[-1][2], b"\x02")
        self.assertTrue(camera.get_noise_cancellation())
        with self.assertRaisesRegex(ValueError, "unsupported audio mode"):
            camera.set_audio_mode("surround")

    def test_invalid_xu_length_uses_the_supplied_fallback(self):
        camera = object.__new__(Camera)
        camera._xu_lengths = {}
        camera._xu_query = lambda *_args: b"\x00\x00"

        self.assertEqual(camera.xu_length(9, 27, fallback=2), 2)
        with self.assertRaisesRegex(CameraError, "invalid XU length"):
            camera.xu_length(9, 28)

    def test_xu_length_does_not_cache_cancellation_as_a_fallback(self):
        camera = object.__new__(Camera)
        camera._xu_lengths = {}

        def cancelled(*_args):
            raise CameraOperationCancelled("cancelled")

        camera._xu_query = cancelled

        with self.assertRaises(CameraOperationCancelled):
            camera.xu_length(9, 27, fallback=2)
        self.assertNotIn((9, 27), camera._xu_lengths)

    def test_read_state_contains_short_firmware_payload_errors(self):
        camera = object.__new__(Camera)
        camera.get_control = lambda _key: 0
        camera.read_video_mode = lambda: "normal"
        camera.get_feature = lambda _bit: False
        camera.get_privacy = lambda: False
        camera.get_auto_exposure = lambda: True
        camera.get_exposure_compensation = lambda: struct.unpack("<h", b"")[0]
        camera.get_tracking_speed = lambda: b""[0]
        camera.get_framing = lambda: "head"
        camera.get_noise_cancellation = lambda: True
        camera.get_audio_mode = lambda: "voice_focus"
        camera.get_manual_iso = lambda: 100
        camera.get_shutter = lambda: 1000
        camera.get_device_information = lambda: {}

        state = camera.read_state()

        self.assertIn("exposure_compensation", state["unavailable"])
        self.assertIn("tracking_speed", state["unavailable"])

    def test_read_state_propagates_operation_cancellation(self):
        camera = object.__new__(Camera)
        camera.get_control = lambda _key: 0

        def cancelled():
            raise CameraOperationCancelled("cancelled")

        camera.read_video_mode = cancelled

        with self.assertRaises(CameraOperationCancelled):
            camera.read_state()

    def test_video_mode_waits_are_cooperatively_cancelled(self):
        camera = object.__new__(Camera)
        camera._cancel_operations = threading.Event()
        camera._last_mode = "unknown"
        camera._last_mode_time = 0.0
        entered_wait = threading.Event()
        errors = []

        def read_transition(*_args):
            entered_wait.set()
            return b"\xff"

        camera.xu_get = read_transition

        def change_mode():
            try:
                camera.set_video_mode("tracking", verify_streaming=True)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=change_mode)
        worker.start()
        self.assertTrue(entered_wait.wait(0.5))
        camera.cancel_pending_operations()
        worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CameraOperationCancelled)
