import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from link_studio.camera import CameraDevice, CameraError, CameraOperationCancelled
from link_studio.constants import ANTI_FLICKER_LABELS, TRACKING_SPEED_MAX
from link_studio.effects import EffectSettings
from link_studio.geometry import contained_rect, frame_region_from_drag
from link_studio.presets import Preset
from link_studio.window import (
    LinkStudioWindow,
    _rediscover_compatibility_formats,
    _start_camera_drain,
    control_dropdown_index,
)


class _FakeSwitch:
    def __init__(self, active=False):
        self.active = active

    def set_active(self, active):
        self.active = active

    def get_active(self):
        return self.active


class _CallbackToggle(_FakeSwitch):
    def __init__(self, active=False, on_change=None):
        super().__init__(active)
        self.on_change = on_change
        self.icon_name = None

    def set_active(self, active):
        changed = self.active != active
        super().set_active(active)
        if changed and self.on_change:
            self.on_change(active)

    def set_icon_name(self, icon_name):
        self.icon_name = icon_name


class _FakeSpinButton:
    def __init__(self, value=0):
        self.value = value

    def set_value(self, value):
        self.value = value

    def get_value_as_int(self):
        return round(self.value)


class _FakeDropDown:
    def __init__(self, selected=0):
        self.selected = selected

    def set_selected(self, selected):
        self.selected = selected

    def set_model(self, model):
        self.model = model

    def get_selected(self):
        return self.selected


class _FakeCamera:
    def __init__(self, refreshed):
        self.refreshed = refreshed
        self.writes = []

    def set_control(self, key, value):
        self.writes.append((key, value))
        return value

    def set_video_mode(self, mode, verify_streaming=False):
        self.writes.append(("mode", mode, verify_streaming))
        return mode

    def read_state(self):
        return dict(self.refreshed)


class _FakePreview:
    def __init__(self):
        self.running = False
        self._settings = EffectSettings()

    @property
    def effect_settings(self):
        return self._settings

    def set_effects(self, **changes):
        self._settings = replace(self._settings, **changes)
        return self._settings


class _BlockingExecutor:
    def __init__(self):
        self.calls = []
        self.waiting = threading.Event()
        self.release = threading.Event()

    def shutdown(self, wait=True, cancel_futures=False):
        self.calls.append((wait, cancel_futures))
        if wait:
            self.waiting.set()
            self.release.wait(1)


class _ClosableCamera:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class _FakeRGBA:
    def __init__(self):
        self.value = ""

    def parse(self, value):
        self.value = value
        return True


def _immediate_submit(_message, operation, on_success=None, **_kwargs):
    result = operation()
    if on_success:
        on_success(result)


class CompatibilityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.device = CameraDevice("/mock/video0", "Link", 0x2E1A, 0x4C01, "usb-port-1")
        self.returned = replace(self.device, path="/mock/video4")
        self.camera = Mock(device=self.device)
        self.clock = 0.0
        self.camera._wait_or_cancel.side_effect = self.wait
        self.camera.reconnect.side_effect = lambda device: setattr(self.camera, "device", device)
        self.discovery = patch(
            "link_studio.window.discover_cameras", return_value=[self.returned]
        ).start()
        self.formats = patch("link_studio.window.discover_capture_formats").start()
        patch("link_studio.window.time.monotonic", side_effect=lambda: self.clock).start()
        self.addCleanup(patch.stopall)
        self.low = {(1280, 720): (24, 30), (1920, 1080): (24, 30)}
        self.high = {(1280, 720): (24, 30, 60), (1920, 1080): (24, 30, 60)}

    def wait(self, seconds):
        self.clock += seconds

    def test_transient_empty_and_old_formats_in_both_directions(self):
        for enabled, old, new in ((True, self.low, self.high), (False, self.high, self.low)):
            self.camera.get_portrait_high_fps.return_value = enabled
            self.formats.side_effect = [{}, old, new]
            self.camera.reset_mock()
            self.assertEqual(_rediscover_compatibility_formats(self.camera, enabled), new)
            self.assertEqual(self.camera.reconnect.call_count, 3)
            self.assertEqual(self.camera.device.path, "/mock/video4")
            self.camera._wait_or_cancel.assert_any_call(8.0)
            self.camera.set_portrait_high_fps.assert_not_called()
            self.camera.close.assert_called_once()

    def test_missing_camera_and_reopen_errors_are_retried(self):
        self.discovery.side_effect = [[], [self.returned], [self.returned]]
        self.camera.reconnect.side_effect = [OSError("not ready"), None]
        self.camera.get_portrait_high_fps.return_value = True
        self.formats.return_value = self.high
        self.assertEqual(_rediscover_compatibility_formats(self.camera, True), self.high)
        self.assertEqual(self.discovery.call_count, 3)

    def test_wrong_port_or_ambiguous_nodes_never_reopen(self):
        for devices in (
            [replace(self.device, bus_info="usb-port-2")],
            [self.returned, self.returned],
        ):
            self.discovery.return_value = devices
            with self.assertRaisesRegex(CameraError, "Timed out"):
                _rediscover_compatibility_formats(self.camera, True)
            self.camera.reconnect.assert_not_called()
            self.formats.assert_not_called()

    def test_empty_stale_partial_formats_and_readback_errors_time_out(self):
        for formats, readback in (
            ({}, True),
            (self.low, True),
            ({(1280, 720): (60,)}, True),
            (self.high, False),
            (self.high, OSError("stale fd")),
        ):
            self.formats.return_value = formats
            self.camera.get_portrait_high_fps.side_effect = (
                readback if isinstance(readback, Exception) else None
            )
            self.camera.get_portrait_high_fps.return_value = readback
            start = self.clock
            with self.assertRaisesRegex(CameraError, "Timed out"):
                _rediscover_compatibility_formats(self.camera, True)
            self.assertLessEqual(self.clock - start, 32.0)
            self.camera.set_portrait_high_fps.assert_not_called()

    def test_cancellation_during_restart_and_polling_is_not_swallowed(self):
        self.camera._wait_or_cancel.side_effect = CameraOperationCancelled("shutdown")
        with self.assertRaises(CameraOperationCancelled):
            _rediscover_compatibility_formats(self.camera, True)
        self.discovery.assert_not_called()
        self.camera._wait_or_cancel.side_effect = self.wait
        self.camera.reconnect.side_effect = CameraOperationCancelled("shutdown")
        with self.assertRaises(CameraOperationCancelled):
            _rediscover_compatibility_formats(self.camera, True)
        self.formats.assert_not_called()


class WindowRegressionTests(unittest.TestCase):
    def compatibility_window(self):
        window = SimpleNamespace(
            _updating=False,
            _compatibility_pending=False,
            _compatibility_failed=False,
            state={"portrait_high_fps": False},
            compatibility_switch=Mock(),
            compatibility_row=Mock(),
            window_title=Mock(),
            preview=SimpleNamespace(running=False),
            recorder=None,
            virtual_camera=None,
            camera=Mock(),
            _toast=Mock(),
            _submit=Mock(),
            _refresh_capture_formats=Mock(),
        )
        return window

    def test_compatibility_refuses_every_active_consumer(self):
        for consumer in ("preview", "recorder", "virtual_camera"):
            window = self.compatibility_window()
            if consumer == "preview":
                window.preview.running = True
            else:
                setattr(window, consumer, object())
            LinkStudioWindow._set_portrait_high_fps(window, True)
            window._submit.assert_not_called()
            window.camera.set_portrait_high_fps.assert_not_called()
            window.compatibility_switch.set_active.assert_called_once_with(False)

    def test_compatibility_programmatic_updates_and_duplicate_requests_are_ignored(self):
        for flag in ("_updating", "_compatibility_pending", "_compatibility_failed"):
            window = self.compatibility_window()
            setattr(window, flag, True)
            LinkStudioWindow._set_portrait_high_fps(window, True)
            window._submit.assert_not_called()

    def test_compatibility_worker_refreshes_only_after_verification(self):
        window = self.compatibility_window()
        window.camera.set_portrait_high_fps.return_value = True
        formats = {(1280, 720): (30, 60)}
        LinkStudioWindow._set_portrait_high_fps(window, True)
        self.assertTrue(window._compatibility_pending)
        _, operation, success, failure = window._submit.call_args.args
        with patch("link_studio.window._rediscover_compatibility_formats", return_value=formats):
            result = operation()
        window._refresh_capture_formats.assert_not_called()
        success(result)
        window._refresh_capture_formats.assert_called_once_with(formats)
        self.assertTrue(window.state["portrait_high_fps"])
        self.assertFalse(window._compatibility_pending)
        self.assertEqual(window.preview.device_path, window.camera.device.path)
        window.camera.set_portrait_high_fps.assert_called_once_with(True)
        window.camera.set_portrait_high_fps.side_effect = RuntimeError("disconnected")
        with patch("link_studio.window.discover_capture_formats") as discover:
            with self.assertRaises(RuntimeError):
                operation()
            discover.assert_not_called()
        failure(RuntimeError("disconnected"))
        self.assertNotIn("portrait_high_fps", window.state)
        self.assertIn("reconnect", window.compatibility_row.set_subtitle.call_args.args[0])

    def test_compatibility_recovery_timeout_is_not_success(self):
        window = self.compatibility_window()
        LinkStudioWindow._set_portrait_high_fps(window, True)
        operation = window._submit.call_args.args[1]
        with (
            patch(
                "link_studio.window._rediscover_compatibility_formats",
                side_effect=CameraError("Timed out rediscovering camera formats"),
            ),
            self.assertRaisesRegex(CameraError, "rediscover"),
        ):
            operation()

    def test_failed_recovery_keeps_preview_blocked_after_video_node_changes(self):
        window = self.compatibility_window()
        window.preview.device_path = "/mock/video0"
        window.preview.start = Mock()
        window._set_preview_stopped_ui = Mock()
        LinkStudioWindow._set_portrait_high_fps(window, True)
        _, operation, _success, failure = window._submit.call_args.args

        def reopened_but_unverified(_camera, _enabled):
            window.camera.device.path = "/mock/video4"
            raise CameraError("Timed out verifying formats")

        with (
            patch(
                "link_studio.window._rediscover_compatibility_formats",
                side_effect=reopened_but_unverified,
            ),
            self.assertRaisesRegex(CameraError, "Timed out") as error,
        ):
            operation()
        failure(error.exception)
        self.assertFalse(window._compatibility_pending)
        self.assertTrue(window._compatibility_failed)
        LinkStudioWindow._preview_toggled(window, _FakeSwitch(True))
        window.preview.start.assert_not_called()
        window._set_preview_stopped_ui.assert_called_once()
        self.assertIn("reopen", window._toast.call_args.args[0])

    def test_compatibility_missing_physical_identity_never_writes(self):
        window = self.compatibility_window()
        window.camera.device.bus_info = ""
        LinkStudioWindow._set_portrait_high_fps(window, True)
        with self.assertRaisesRegex(CameraError, "physical port"):
            window._submit.call_args.args[1]()
        window.camera.set_portrait_high_fps.assert_not_called()

    def test_preview_cannot_start_during_compatibility_change(self):
        window = SimpleNamespace(
            _updating=False,
            _compatibility_pending=True,
            _compatibility_failed=False,
            preview=Mock(),
            _toast=Mock(),
            _set_preview_stopped_ui=Mock(),
        )
        LinkStudioWindow._preview_toggled(window, _FakeSwitch(True))
        window.preview.start.assert_not_called()
        window._set_preview_stopped_ui.assert_called_once()

    def test_format_refresh_preserves_valid_selection_and_handles_removed_modes(self):
        window = SimpleNamespace(
            _stream_resolutions=[(1280, 720), (1920, 1080)],
            _stream_rates=[30, 60],
            resolution_dropdown=_FakeDropDown(1),
            fps_dropdown=_FakeDropDown(0),
            preview=SimpleNamespace(config=None),
            _changing_stream_controls=False,
        )
        fake_gtk = SimpleNamespace(StringList=SimpleNamespace(new=lambda values: values))
        with patch("link_studio.window.Gtk", fake_gtk):
            LinkStudioWindow._refresh_capture_formats(
                window, {(720, 1280): (30, 60), (1920, 1080): (24, 30, 60)}
            )
            self.assertEqual(window.preview.config.width, 1920)
            self.assertEqual(window.preview.config.fps, 30)
            window.fps_dropdown.set_selected(2)
            LinkStudioWindow._refresh_capture_formats(window, {(1280, 720): (24, 30)})
        self.assertEqual(window.preview.config.width, 1280)
        self.assertEqual(window.preview.config.fps, 30)
        self.assertFalse(window._changing_stream_controls)

    def test_camera_drain_returns_before_the_worker_and_closes_afterward(self):
        executor = _BlockingExecutor()
        camera = _ClosableCamera()

        thread = _start_camera_drain(executor, camera)

        self.assertTrue(executor.waiting.wait(0.5))
        self.assertEqual(executor.calls[0], (False, True))
        self.assertFalse(camera.closed.is_set())
        executor.release.set()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(camera.closed.is_set())

    def test_anti_flicker_auto_and_full_tracking_speed_are_representable(self):
        self.assertEqual(ANTI_FLICKER_LABELS[3], "Auto")
        self.assertEqual(control_dropdown_index("anti_flicker", 3), 3)
        self.assertEqual(TRACKING_SPEED_MAX, 255)

    def test_contained_region_mapping_excludes_letterbox_bars(self):
        content = contained_rect(1000, 1000, 1600, 900)
        self.assertEqual(content, (0.0, 218.75, 1000.0, 562.5))
        self.assertEqual(
            frame_region_from_drag((0, 0), (1000, 1000), (1000, 1000), (1600, 900)),
            (0.0, 0.0, 1.0, 1.0),
        )
        self.assertEqual(
            frame_region_from_drag((250, 218.75), (750, 500), (1000, 1000), (1600, 900)),
            (0.25, 0.0, 0.5, 0.5),
        )

    def test_control_widgets_resync_without_emitting_user_operations(self):
        switch = _FakeSwitch()
        spin = _FakeSpinButton()
        dropdown = _FakeDropDown()
        window = SimpleNamespace(
            _updating=False,
            _control_widgets={
                "hdr": switch,
                "zoom": spin,
                "anti_flicker": dropdown,
            },
        )

        fake_gtk = SimpleNamespace(
            Switch=_FakeSwitch,
            SpinButton=_FakeSpinButton,
            DropDown=_FakeDropDown,
        )
        with patch("link_studio.window.Gtk", fake_gtk):
            LinkStudioWindow._sync_control_widgets(
                window, {"hdr": True, "zoom": 175, "anti_flicker": 3}
            )

        self.assertTrue(switch.get_active())
        self.assertEqual(spin.get_value_as_int(), 175)
        self.assertEqual(dropdown.get_selected(), 3)
        self.assertFalse(window._updating)

    def test_scene_preset_refreshes_widgets_from_hardware_state(self):
        camera = _FakeCamera({"zoom": 142, "anti_flicker": 3, "mode": "normal"})
        sync = Mock()
        modes = Mock()
        window = SimpleNamespace(
            presets=SimpleNamespace(
                presets=[Preset("Scene", {"zoom": 140, "anti_flicker": 3, "mode": "normal"})]
            ),
            camera=camera,
            preview=SimpleNamespace(running=False),
            state={},
            _submit=_immediate_submit,
            _sync_control_widgets=sync,
            _sync_mode_buttons=modes,
        )

        LinkStudioWindow._apply_preset(window, 0)

        self.assertEqual(window.state["zoom"], 142)
        sync.assert_called_once_with({"zoom": 142, "anti_flicker": 3, "mode": "normal"})
        modes.assert_called_once_with("normal")

    def test_scene_preset_restores_regions_and_unregistered_effect_widgets(self):
        software = {
            "background_color": "#123456",
            "background_image": "/home/test/studio.png",
            "key_color": "#abcdef",
            "tracking_area": [0.1, 0.2, 0.7, 0.6],
            "pause_areas": [[0.2, 0.3, 0.1, 0.1]],
        }
        preview = _FakePreview()
        effects_sync = Mock()
        window = SimpleNamespace(
            presets=SimpleNamespace(
                presets=[Preset("Scene", {"mode": "normal", "software_effects": software})]
            ),
            camera=_FakeCamera({"mode": "normal"}),
            preview=preview,
            state={},
            _submit=_immediate_submit,
            _sync_control_widgets=Mock(),
            _sync_software_effect_widgets=effects_sync,
            _sync_mode_buttons=Mock(),
        )

        LinkStudioWindow._apply_preset(window, 0)

        settings = preview.effect_settings
        self.assertEqual(settings.tracking_area, (0.1, 0.2, 0.7, 0.6))
        self.assertEqual(settings.pause_areas, ((0.2, 0.3, 0.1, 0.1),))
        effects_sync.assert_called_once_with(settings)

    def test_portrait_preset_stops_fixed_geometry_consumers_before_effect_restore(self):
        events = []
        preview = _FakePreview()
        original_set_effects = preview.set_effects

        def set_effects(**changes):
            events.append("effects")
            return original_set_effects(**changes)

        preview.set_effects = set_effects
        window = SimpleNamespace(
            presets=SimpleNamespace(
                presets=[
                    Preset(
                        "Portrait",
                        {
                            "mode": "normal",
                            "software_effects": {"orientation": "rotate_right"},
                        },
                    )
                ]
            ),
            camera=_FakeCamera({"mode": "normal"}),
            preview=preview,
            state={},
            record_button=_CallbackToggle(
                True, lambda active: events.append("record-stopped") if not active else None
            ),
            virtual_camera_switch=_CallbackToggle(
                True, lambda active: events.append("virtual-stopped") if not active else None
            ),
            _submit=_immediate_submit,
            _sync_control_widgets=Mock(),
            _sync_software_effect_widgets=Mock(),
            _sync_mode_buttons=Mock(),
        )
        window._stop_frame_consumers = lambda: LinkStudioWindow._stop_frame_consumers(window)

        LinkStudioWindow._apply_preset(window, 0)

        self.assertEqual(events, ["record-stopped", "virtual-stopped", "effects"])
        self.assertFalse(window.record_button.get_active())
        self.assertFalse(window.virtual_camera_switch.get_active())
        self.assertEqual(preview.effect_settings.orientation, "rotate_right")

    def test_software_effect_sync_updates_regions_colors_and_image(self):
        background = Mock()
        key = Mock()
        label = Mock()
        overlay = Mock()
        window = SimpleNamespace(
            _updating=False,
            background_color_button=background,
            key_color_button=key,
            background_image_label=label,
            region_overlay=overlay,
            _tracking_area=(0.0, 0.0, 1.0, 1.0),
            _pause_areas=[],
        )
        settings = EffectSettings(
            background_color="#123456",
            background_image="/home/test/scene.png",
            key_color="#abcdef",
            tracking_area=(0.1, 0.2, 0.7, 0.6),
            pause_areas=((0.2, 0.3, 0.1, 0.1),),
        )

        with patch("link_studio.window.Gdk", SimpleNamespace(RGBA=_FakeRGBA)):
            LinkStudioWindow._sync_software_effect_widgets(window, settings)

        self.assertEqual(background.set_rgba.call_args.args[0].value, "#123456")
        self.assertEqual(key.set_rgba.call_args.args[0].value, "#abcdef")
        label.set_label.assert_called_once_with("scene.png")
        self.assertEqual(window._tracking_area, settings.tracking_area)
        self.assertEqual(window._pause_areas, list(settings.pause_areas))
        overlay.queue_draw.assert_called_once_with()

    def test_stream_failure_retires_stale_frame_and_live_ui_state(self):
        preview = SimpleNamespace(
            running=False,
            take_error=Mock(return_value="device disconnected"),
            take_latest=Mock(),
            stop=Mock(),
        )
        teardown_events = []
        window = SimpleNamespace(
            _closed=False,
            _preview_poll_source=42,
            _updating=False,
            preview=preview,
            latest_frame=object(),
            picture=Mock(),
            preview_placeholder=Mock(),
            preview_placeholder_label=Mock(),
            preview_status=Mock(),
            compact_status=Mock(),
            _toast=Mock(),
        )
        window.record_button = _CallbackToggle(
            True,
            lambda active: teardown_events.append(("record", active, window._updating)),
        )
        window.virtual_camera_switch = _CallbackToggle(
            True,
            lambda active: teardown_events.append(("virtual", active, window._updating)),
        )
        window.preview_toggle = _CallbackToggle(True)
        window._stop_frame_consumers = lambda: LinkStudioWindow._stop_frame_consumers(window)
        window._set_preview_stopped_ui = lambda status, placeholder: (
            LinkStudioWindow._set_preview_stopped_ui(window, status, placeholder)
        )

        self.assertFalse(LinkStudioWindow._poll_preview(window))

        self.assertIsNone(window.latest_frame)
        self.assertEqual(window._preview_poll_source, 0)
        preview.stop.assert_called_once_with()
        preview.take_latest.assert_not_called()
        window.preview_status.set_label.assert_called_once_with("Stream error")
        self.assertFalse(window.preview_toggle.get_active())
        self.assertEqual(window.preview_toggle.icon_name, "media-playback-start-symbolic")
        self.assertFalse(window.record_button.get_active())
        self.assertFalse(window.virtual_camera_switch.get_active())
        self.assertEqual(
            teardown_events,
            [("record", False, False), ("virtual", False, False)],
        )

    def test_failed_stream_format_uses_complete_stopped_preview_state(self):
        preview = SimpleNamespace(
            running=True,
            config=None,
            stop=Mock(),
            start=Mock(side_effect=RuntimeError("unsupported format")),
        )
        stopped_ui = Mock()
        window = SimpleNamespace(
            _changing_stream_controls=False,
            _stream_resolutions=[(3840, 2160)],
            _stream_rates=[30],
            _all_stream_rates=[30],
            resolution_dropdown=_FakeDropDown(),
            fps_dropdown=_FakeDropDown(),
            capture_formats={(3840, 2160): (30,)},
            virtual_camera=None,
            preview=preview,
            _toast=Mock(),
            _stop_preview_poll=Mock(),
            _stop_frame_consumers=Mock(),
            _set_preview_stopped_ui=stopped_ui,
        )

        LinkStudioWindow._stream_config_changed(window)

        preview.stop.assert_called_once_with()
        window._stop_frame_consumers.assert_called_once_with()
        window._stop_preview_poll.assert_called_once_with()
        stopped_ui.assert_called_once_with("Ready", "Preview is off")

    def test_stream_format_change_stops_consumers_before_pipeline_restart(self):
        events = []
        preview = SimpleNamespace(
            running=True,
            config=None,
            output_label="3840x2160 · 30 fps",
            stop=lambda: events.append("preview-stopped"),
            start=lambda: events.append("preview-started"),
        )
        window = SimpleNamespace(
            _changing_stream_controls=False,
            _stream_resolutions=[(3840, 2160)],
            _stream_rates=[30],
            _all_stream_rates=[30],
            resolution_dropdown=_FakeDropDown(),
            fps_dropdown=_FakeDropDown(),
            capture_formats={(3840, 2160): (30,)},
            preview=preview,
            record_button=_CallbackToggle(
                True, lambda active: events.append("record-stopped") if not active else None
            ),
            virtual_camera_switch=_CallbackToggle(
                True, lambda active: events.append("virtual-stopped") if not active else None
            ),
            preview_status=Mock(),
            compact_status=Mock(),
            _toast=Mock(),
            _stop_preview_poll=Mock(),
            _set_preview_stopped_ui=Mock(),
        )
        window._stop_frame_consumers = lambda: LinkStudioWindow._stop_frame_consumers(window)

        LinkStudioWindow._stream_config_changed(window)

        self.assertEqual(
            events,
            ["record-stopped", "virtual-stopped", "preview-stopped", "preview-started"],
        )
        self.assertFalse(window.record_button.get_active())
        self.assertFalse(window.virtual_camera_switch.get_active())
        window.preview_status.set_label.assert_called_once_with(preview.output_label)

    def test_scheduled_job_completion_is_ignored_after_window_closes(self):
        class ImmediateFuture:
            def add_done_callback(self, callback):
                callback(self)

            def result(self):
                return "done"

        executor = SimpleNamespace(submit=Mock(return_value=ImmediateFuture()))
        scheduled = []
        success = Mock()
        window = SimpleNamespace(
            _closed=False,
            executor=executor,
            _job_started=Mock(),
            _job_finished=Mock(),
            _toast=Mock(),
        )

        with patch("link_studio.window.GLib.idle_add", side_effect=scheduled.append):
            LinkStudioWindow._submit(window, "Completed", lambda: "done", on_success=success)

        self.assertEqual(len(scheduled), 1)
        window._closed = True
        self.assertFalse(scheduled[0]())
        window._job_finished.assert_not_called()
        success.assert_not_called()
        window._toast.assert_not_called()

    def test_screenshot_rejects_a_stale_frame_after_preview_stops(self):
        window = SimpleNamespace(
            latest_frame=object(),
            preview=SimpleNamespace(running=False),
            _toast=Mock(),
        )

        LinkStudioWindow.take_screenshot(window)

        window._toast.assert_called_once_with("Start the preview before taking a screenshot")

    def test_color_preset_refreshes_widgets_from_hardware_state(self):
        camera = _FakeCamera({"brightness": 61, "anti_flicker": 3})
        sync = Mock()
        window = SimpleNamespace(
            color_presets=SimpleNamespace(
                presets=[Preset("Color", {"brightness": 60, "anti_flicker": 3})]
            ),
            camera=camera,
            state={},
            _submit=_immediate_submit,
            _sync_control_widgets=sync,
        )

        LinkStudioWindow._apply_color_preset(window, 0)

        self.assertEqual(window.state["brightness"], 61)
        sync.assert_called_once_with({"brightness": 61, "anti_flicker": 3})


if __name__ == "__main__":
    unittest.main()
