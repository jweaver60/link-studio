import ctypes
import struct
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from link_studio.camera import (
    UVCIOC_CTRL_QUERY,
    VIDIOC_G_CTRL,
    VIDIOC_QUERYCAP,
    VIDIOC_S_CTRL,
    Camera,
    CameraDevice,
    CameraError,
    CameraOperationCancelled,
    _UvcXuControlQuery,
)
from link_studio.constants import AUDIO_MODES, STANDARD_CONTROLS, VIDEO_MODES


class OriginalLinkCompatibilityTests(unittest.TestCase):
    def camera(self, mask=0xC40C, product=0x4C01, vendor=0x2E1A):
        camera = object.__new__(Camera)
        camera.device = CameraDevice("/not-a-device", "Mock Link", vendor, product)
        camera._lock = threading.RLock()
        camera._xu_lengths = {(9, 27): 2}  # Must not trust even a cached fallback.
        camera._xu_query = Mock(side_effect=[b"\x02\x00", mask.to_bytes(2, "little")])
        camera.xu_set = Mock()
        return camera

    def test_model_and_vendor_gate_before_any_usb_operation(self):
        for vendor, product in ((0x2E1A, 0x4C04), (0x1234, 0x4C01)):
            camera = self.camera(product=product, vendor=vendor)
            for operation in (
                camera.get_portrait_high_fps,
                lambda camera=camera: camera.set_portrait_high_fps(True),
            ):
                with self.assertRaises(CameraError):
                    operation()
            camera._xu_query.assert_not_called()
            camera.xu_set.assert_not_called()

    def test_only_bit_five_changes_and_full_word_is_verified(self):
        for enabled, before, after in ((True, 0xC40C, 0xC42C), (False, 0xFFFF, 0xFFDF)):
            camera = self.camera()
            camera._xu_query.side_effect = [
                b"\x02\x00",
                before.to_bytes(2, "little"),
                b"\x02\x00",
                after.to_bytes(2, "little"),
            ]
            self.assertEqual(camera.set_portrait_high_fps(enabled), enabled)
            camera.xu_set.assert_called_once_with(9, 27, after.to_bytes(2, "little"))
            self.assertEqual(before & 0x0400, after & 0x0400)
            self.assertTrue(
                all(call.args[:2] == (9, 27) for call in camera._xu_query.call_args_list)
            )

    def test_idempotent_in_both_directions(self):
        for enabled, mask in ((True, 0xFFFF), (False, 0xFFDF)):
            camera = self.camera(mask)
            self.assertEqual(camera.set_portrait_high_fps(enabled), enabled)
            camera.xu_set.assert_not_called()

    def test_invalid_lengths_never_write_or_fall_back(self):
        for responses in (
            [b"\x01\x00"],
            [b"\x03\x00"],
            [b""],
            [OSError("GET_LEN rejected")],
            [b"\x02\x00", b""],
            [b"\x02\x00", b"123"],
        ):
            camera = self.camera()
            camera._xu_query.side_effect = responses
            with self.assertRaises((CameraError, OSError)):
                camera.set_portrait_high_fps(True)
            camera.xu_set.assert_not_called()

    def test_write_rejection_and_failed_or_mismatched_readback_are_errors(self):
        camera = self.camera()
        camera.xu_set.side_effect = OSError("SET rejected")
        with self.assertRaises(OSError):
            camera.set_portrait_high_fps(True)
        for readback in (b"\x0c\xc4", b"\x2d\xc4", b"", OSError("disconnected")):
            camera = self.camera()
            camera._xu_query.side_effect = [b"\x02\x00", b"\x0c\xc4", b"\x02\x00", readback]
            with self.assertRaises((CameraError, OSError)):
                camera.set_portrait_high_fps(True)


class CameraReconnectTests(unittest.TestCase):
    def setUp(self):
        self.camera = object.__new__(Camera)
        self.camera.device = CameraDevice("/mock/video0", "Link", 0x2E1A, 0x4C01, "usb-port-1")
        self.camera._fd = 10
        self.camera._lock = threading.RLock()
        self.camera._cancel_operations = threading.Event()
        self.camera._xu_lengths = {(9, 27): 2}
        self.device = replace(self.camera.device, path="/mock/video4")

        def query_capability(request, capability):
            self.assertEqual(request, VIDIOC_QUERYCAP)
            capability.bus_info = b"usb-port-1"

        self.camera._ioctl = Mock(side_effect=query_capability)
        self.open = patch("link_studio.camera.os.open", return_value=11).start()
        self.close = patch("link_studio.camera.os.close").start()
        self.addCleanup(patch.stopall)

    def test_reopens_same_port_changed_node_and_clears_cached_lengths(self):
        self.camera.reconnect(self.device)
        self.close.assert_called_once_with(10)
        self.open.assert_called_once()
        self.assertEqual(self.camera._fd, 11)
        self.assertEqual(self.camera.device, self.device)
        self.assertEqual(self.camera._xu_lengths, {})
        self.camera._ioctl.assert_called_once()

    def test_rejects_wrong_or_missing_identity_before_closing(self):
        for device in (
            replace(self.device, vendor_id=0x1234),
            replace(self.device, product_id=0x4C04),
            replace(self.device, bus_info="usb-port-2"),
            replace(self.device, bus_info=""),
        ):
            with self.assertRaisesRegex(CameraError, "different or unidentified"):
                self.camera.reconnect(device)
        self.camera.device = replace(self.camera.device, bus_info="")
        with self.assertRaises(CameraError):
            self.camera.reconnect(replace(self.device, bus_info=""))
        self.open.assert_not_called()
        self.close.assert_not_called()

    def test_failed_open_leaves_closed_fd_and_can_be_retried(self):
        self.open.side_effect = [OSError("not ready"), 11]
        with self.assertRaises(OSError):
            self.camera.reconnect(self.device)
        self.assertEqual(self.camera._fd, -1)
        self.camera.reconnect(self.device)
        self.assertEqual(self.camera._fd, 11)
        self.close.assert_called_once_with(10)

    def test_reused_video_node_is_rejected_and_new_fd_closed(self):
        self.camera._ioctl.side_effect = lambda _request, cap: setattr(
            cap, "bus_info", b"usb-port-2"
        )
        with self.assertRaisesRegex(CameraError, "physical port"):
            self.camera.reconnect(self.device)
        self.assertEqual(self.camera._fd, -1)
        self.assertEqual([call.args[0] for call in self.close.call_args_list], [10, 11])
        self.assertEqual(self.camera.device.path, "/mock/video0")

    def test_query_failure_and_cancellation_close_new_fd(self):
        for error in (OSError("disconnected"), CameraOperationCancelled("shutdown")):
            self.camera._ioctl.side_effect = error
            with self.assertRaises(type(error)):
                self.camera.reconnect(self.device)
            self.assertEqual(self.camera._fd, -1)
        self.camera.cancel_pending_operations()
        self.open.reset_mock()
        with self.assertRaises(CameraOperationCancelled):
            self.camera.reconnect(self.device)
        self.open.assert_not_called()


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
