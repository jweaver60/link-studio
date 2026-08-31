import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from link_studio.constants import ANTI_FLICKER_LABELS, TRACKING_SPEED_MAX
from link_studio.geometry import contained_rect, frame_region_from_drag
from link_studio.presets import Preset
from link_studio.window import LinkStudioWindow, control_dropdown_index


class _FakeSwitch:
    def __init__(self, active=False):
        self.active = active

    def set_active(self, active):
        self.active = active

    def get_active(self):
        return self.active


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


def _immediate_submit(_message, operation, on_success=None, **_kwargs):
    result = operation()
    if on_success:
        on_success(result)


class WindowRegressionTests(unittest.TestCase):
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
