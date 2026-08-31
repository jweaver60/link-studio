import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from link_studio.constants import ANTI_FLICKER_LABELS, TRACKING_SPEED_MAX
from link_studio.effects import EffectSettings
from link_studio.geometry import contained_rect, frame_region_from_drag
from link_studio.presets import Preset
from link_studio.window import LinkStudioWindow, _start_camera_drain, control_dropdown_index


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


class WindowRegressionTests(unittest.TestCase):
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
            _set_preview_stopped_ui=stopped_ui,
        )

        LinkStudioWindow._stream_config_changed(window)

        preview.stop.assert_called_once_with()
        window._stop_preview_poll.assert_called_once_with()
        stopped_ui.assert_called_once_with("Ready", "Preview is off")

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
