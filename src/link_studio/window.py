from __future__ import annotations

import concurrent.futures
import io
import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk

from .audio import (
    discover_audio_sources,
    preferred_audio_source,
    set_source_mute,
    set_source_volume,
)
from .camera import (
    FEATURE_GESTURE_ZOOM,
    FEATURE_HDR,
    FEATURE_MIRROR,
    Camera,
)
from .constants import (
    ANTI_FLICKER_LABELS,
    APP_NAME,
    FRAMING_MODES,
    STANDARD_CONTROLS,
    TRACKING_SPEED_MAX,
    VIDEO_MODES,
)
from .diagnostics import create_support_bundle
from .effects import Rect, TrackingTarget
from .geometry import contained_rect, frame_region_from_drag
from .meeting import (
    AudioNoteRecorder,
    MeetingResult,
    default_meeting_dir,
    default_whisper_model,
    transcribe_meeting,
    whisper_available,
)
from .presets import ColorPresetStore, Preset, PresetStore
from .preview import (
    Frame,
    PreviewConfig,
    PreviewStream,
    Recorder,
    VirtualCameraPublisher,
    discover_capture_formats,
    discover_virtual_camera_devices,
)
from .remote import RemoteServer
from .storage import StorageSettings
from .teleprompter import ScriptStore, TeleprompterWindow
from .theme import OmarchyThemeBridge, Palette

LOGGER = logging.getLogger("link_studio.window")

_DROPDOWN_VALUES: dict[str, tuple[object, ...]] = {
    "anti_flicker": tuple(range(len(ANTI_FLICKER_LABELS))),
    "framing": tuple(FRAMING_MODES),
    "orientation": ("identity", "vertical_flip", "rotate_right", "rotate_left", "rotate_180"),
    "effect_mode": (
        "none",
        "background_blur",
        "bokeh",
        "background_replace",
        "green_screen",
        "beauty",
        "makeup",
        "smart_whiteboard",
    ),
    "audio_mode": ("voice_focus", "voice_suppression", "music_balance"),
}


def control_dropdown_index(key: str, value: object) -> int | None:
    choices = _DROPDOWN_VALUES.get(key)
    if not choices:
        return None
    try:
        return choices.index(value)
    except ValueError:
        return None


def _videos_dir() -> Path:
    value = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    return Path(value) if value else Path.home() / "Videos"


def _pictures_dir() -> Path:
    value = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
    return Path(value) if value else Path.home() / "Pictures"


def _downloads_dir() -> Path:
    value = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    return Path(value) if value else Path.home() / "Downloads"


class LinkStudioWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application, camera: Camera, start_preview: bool = True):
        super().__init__(
            application=application,
            title=APP_NAME,
            default_width=1280,
            default_height=800,
        )
        self.camera = camera
        self.state = camera.read_state()
        self.preview = PreviewStream(camera.device.path)
        self.latest_frame: Frame | None = None
        self.recorder: Recorder | None = None
        self.virtual_camera: VirtualCameraPublisher | None = None
        self.virtual_devices = discover_virtual_camera_devices()
        self.capture_formats = discover_capture_formats(camera.device.path)
        self._stream_resolutions = sorted(
            self.capture_formats,
            key=lambda resolution: resolution[0] * resolution[1],
        ) or [(1280, 720), (1920, 1080), (1920, 1440), (3840, 2160)]
        self._all_stream_rates = sorted(
            {rate for rates in self.capture_formats.values() for rate in rates}
        ) or [24, 25, 30]
        self._stream_rates = list(
            self.capture_formats.get((1280, 720), tuple(self._all_stream_rates))
        )
        self._changing_stream_controls = False
        self.presets = PresetStore()
        self.color_presets = ColorPresetStore()
        self.script_store = ScriptStore()
        self.storage_settings = StorageSettings()
        self.audio_sources = discover_audio_sources()
        self.selected_audio_source = preferred_audio_source(self.audio_sources)
        self.ai_recorder = AudioNoteRecorder(
            audio_source=self.selected_audio_source.name if self.selected_audio_source else None
        )
        self.last_meeting_result: MeetingResult | None = None
        self._ai_recording_timer = 0
        self._preview_poll_source = 0
        self._teleprompter_windows: list[TeleprompterWindow] = []
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="link-camera"
        )
        self._closed = False
        self._updating = False
        self._pending_sources: dict[str, int] = {}
        self._jobs = 0
        self._job_lock = threading.Lock()
        self._mode_buttons: dict[str, Gtk.ToggleButton] = {}
        self._compact_mode_buttons: dict[str, Gtk.ToggleButton] = {}
        self._control_widgets: dict[str, Gtk.Widget] = {}
        self._palette: Palette | None = None
        self.remote = RemoteServer(self._remote_state, self._remote_action)
        self._region_edit_mode: str | None = None
        self._drag_start: tuple[float, float] | None = None
        self._drag_region: Rect | None = None
        self._tracking_area: Rect = (0.0, 0.0, 1.0, 1.0)
        self._pause_areas: list[Rect] = []
        self._last_tracking_move = 0.0

        self._build_ui()
        self.preview.processor.set_tracking_callback(self._tracking_target_received)
        self.theme_bridge = OmarchyThemeBridge(self._theme_applied)
        self.theme_bridge.start()
        self.connect("close-request", self._on_close_request)
        if start_preview:
            GLib.idle_add(self._activate_preview)
        if self.presets.default_index is not None:
            GLib.timeout_add(1200 if start_preview else 100, self._apply_default_preset)

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(
            title=APP_NAME,
            subtitle=f"{self.camera.device.model} · {self.camera.device.path}",
        )
        header.set_title_widget(self.window_title)

        self.preview_toggle = Gtk.ToggleButton(
            icon_name="media-playback-start-symbolic", tooltip_text="Start or stop preview (Ctrl+P)"
        )
        self.preview_toggle.connect("toggled", self._preview_toggled)
        header.pack_start(self.preview_toggle)

        self.compact_button = Gtk.ToggleButton(
            icon_name="view-restore-symbolic", tooltip_text="Toggle compact toolbar (Ctrl+M)"
        )
        self.compact_button.connect("toggled", self._compact_toggled)
        header.pack_start(self.compact_button)

        self.busy_spinner = Gtk.Spinner(spinning=False, visible=False)
        header.pack_start(self.busy_spinner)

        screenshot_button = Gtk.Button(
            icon_name="camera-photo-symbolic", tooltip_text="Take screenshot (Ctrl+Shift+S)"
        )
        screenshot_button.connect("clicked", lambda *_args: self.take_screenshot())
        header.pack_end(screenshot_button)

        self.record_button = Gtk.ToggleButton(
            icon_name="media-record-symbolic", tooltip_text="Start or stop recording (Ctrl+R)"
        )
        self.record_button.connect("toggled", self._record_toggled)
        header.pack_end(self.record_button)

        menu = Gio.Menu()
        menu.append("Export Support Bundle", "app.support-bundle")
        menu.append("About Link Studio", "app.about")
        menu.append("Quit", "app.quit")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        split.set_position(860)
        split.set_resize_start_child(True)
        split.set_shrink_start_child(False)
        split.set_resize_end_child(False)
        split.set_shrink_end_child(False)
        split.set_start_child(self._build_preview_panel())
        split.set_end_child(self._build_controls_panel())
        self.full_view = split
        self.main_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.main_stack.add_named(split, "full")
        self.main_stack.add_named(self._build_compact_bar(), "compact")
        self.main_stack.set_visible_child_name("full")
        toolbar.set_content(self.main_stack)
        self.set_content(self.toast_overlay)

        self._install_actions()

    def _install_actions(self) -> None:
        actions = {
            "screenshot": lambda *_args: self.take_screenshot(),
            "preview": lambda *_args: self.preview_toggle.set_active(
                not self.preview_toggle.get_active()
            ),
            "record": lambda *_args: self.record_button.set_active(
                not self.record_button.get_active()
            ),
            "compact": lambda *_args: self.compact_button.set_active(
                not self.compact_button.get_active()
            ),
        }
        for name, callback in actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def _build_compact_bar(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)
        first = None
        current = str(self.state.get("mode", "normal"))
        for key in ("normal", "tracking", "whiteboard", "overhead", "deskview"):
            button = Gtk.ToggleButton(label=VIDEO_MODES[key][2])
            if first:
                button.set_group(first)
            else:
                first = button
            button.set_active(key == current or (current not in VIDEO_MODES and key == "normal"))
            button.connect("toggled", self._compact_mode_toggled, key)
            self._compact_mode_buttons[key] = button
            box.append(button)
        self.compact_status = Gtk.Label(label="Ready")
        self.compact_status.add_css_class("dim-label")
        box.append(self.compact_status)
        return box

    def _build_preview_panel(self) -> Gtk.Widget:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        panel.set_margin_top(16)
        panel.set_margin_bottom(16)
        panel.set_margin_start(16)
        panel.set_margin_end(16)

        self.preview_overlay = Gtk.Overlay(vexpand=True, hexpand=True)
        self.preview_overlay.add_css_class("link-preview-frame")
        self.picture = Gtk.Picture(
            can_shrink=True,
            content_fit=Gtk.ContentFit.CONTAIN,
            vexpand=True,
            hexpand=True,
        )
        self.preview_overlay.set_child(self.picture)

        self.preview_placeholder = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.CENTER,
        )
        self.preview_placeholder.add_css_class("link-preview-placeholder")
        placeholder_icon = Gtk.Image.new_from_icon_name("camera-web-symbolic")
        placeholder_icon.set_pixel_size(64)
        self.preview_placeholder.append(placeholder_icon)
        self.preview_placeholder_label = Gtk.Label(label="Preview is off")
        self.preview_placeholder_label.add_css_class("title-2")
        self.preview_placeholder.append(self.preview_placeholder_label)
        self.preview_overlay.add_overlay(self.preview_placeholder)
        self.region_overlay = Gtk.DrawingArea(hexpand=True, vexpand=True, can_target=False)
        self.region_overlay.set_draw_func(self._draw_regions)
        self.preview_overlay.add_overlay(self.region_overlay)
        region_drag = Gtk.GestureDrag()
        region_drag.connect("drag-begin", self._region_drag_begin)
        region_drag.connect("drag-update", self._region_drag_update)
        region_drag.connect("drag-end", self._region_drag_end)
        self.preview_overlay.add_controller(region_drag)
        panel.append(self.preview_overlay)

        mode_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True,
        )
        mode_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        mode_bar.add_css_class("link-mode-bar")
        first_button = None
        current_mode = str(self.state.get("mode", "normal"))
        for key in ("normal", "tracking", "whiteboard", "overhead", "deskview"):
            label = VIDEO_MODES[key][2]
            button = Gtk.ToggleButton(label=label)
            button.add_css_class("flat")
            if first_button:
                button.set_group(first_button)
            else:
                first_button = button
            self._mode_buttons[key] = button
            button.set_active(
                key == current_mode or (current_mode not in VIDEO_MODES and key == "normal")
            )
            button.connect("toggled", self._mode_toggled, key)
            mode_bar.append(button)
        mode_scroll.set_child(mode_bar)
        panel.append(mode_scroll)

        stream_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        stream_bar.set_halign(Gtk.Align.CENTER)
        self.resolution_dropdown = Gtk.DropDown.new_from_strings(
            [f"{width} × {height}" for width, height in self._stream_resolutions]
        )
        if (1280, 720) in self._stream_resolutions:
            self.resolution_dropdown.set_selected(self._stream_resolutions.index((1280, 720)))
        self.resolution_dropdown.set_tooltip_text("Preview and recording resolution")
        self.resolution_dropdown.connect("notify::selected", self._stream_config_changed)
        stream_bar.append(self.resolution_dropdown)
        self.fps_dropdown = Gtk.DropDown.new_from_strings(
            [f"{rate} fps" for rate in self._stream_rates]
        )
        if 30 in self._stream_rates:
            self.fps_dropdown.set_selected(self._stream_rates.index(30))
        self.fps_dropdown.connect("notify::selected", self._stream_config_changed)
        stream_bar.append(self.fps_dropdown)
        self.preview_status = Gtk.Label(label="Ready", xalign=0)
        self.preview_status.add_css_class("dim-label")
        stream_bar.append(self.preview_status)
        panel.append(stream_bar)
        return panel

    def _build_controls_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(390, -1)
        self.control_stack = Adw.ViewStack(vexpand=True)
        pages = (
            ("presets", "Presets", "view-grid-symbolic", self._build_presets_page()),
            ("view", "View", "find-location-symbolic", self._build_view_page()),
            ("image", "Image", "image-x-generic-symbolic", self._build_image_page()),
            ("smart", "Smart", "system-search-symbolic", self._build_smart_page()),
            ("effects", "Effects", "applications-graphics-symbolic", self._build_effects_page()),
            ("audio", "Audio", "audio-input-microphone-symbolic", self._build_audio_page()),
            ("tools", "Tools", "applications-utilities-symbolic", self._build_tools_page()),
            ("device", "Device", "emblem-system-symbolic", self._build_device_page()),
        )
        for name, title, icon, child in pages:
            self.control_stack.add_titled_with_icon(child, name, title, icon)
        switcher = Adw.ViewSwitcher(stack=self.control_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_margin_top(10)
        switcher.set_margin_bottom(8)
        switcher.set_margin_start(8)
        switcher.set_margin_end(8)
        box.append(switcher)
        box.append(Gtk.Separator())
        box.append(self.control_stack)
        return box

    def _page(self) -> tuple[Gtk.ScrolledWindow, Gtk.Box]:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(24)
        content.set_margin_start(18)
        content.set_margin_end(18)
        scroll.set_child(content)
        return scroll, content

    def _group(self, title: str, description: str | None = None) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title)
        if description:
            group.set_description(description)
        return group

    def _action_row(
        self, title: str, subtitle: str | None = None, prefix_icon: str | None = None
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        if prefix_icon:
            row.add_prefix(Gtk.Image.new_from_icon_name(prefix_icon))
        return row

    def _switch_row(
        self,
        group: Adw.PreferencesGroup,
        key: str,
        title: str,
        initial: bool,
        callback: Callable[[bool], None],
        subtitle: str | None = None,
    ) -> Gtk.Switch:
        row = self._action_row(title, subtitle)
        switch = Gtk.Switch(active=initial, valign=Gtk.Align.CENTER)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)

        def changed(widget: Gtk.Switch, _param: object) -> None:
            if not self._updating:
                callback(widget.get_active())

        switch.connect("notify::active", changed)
        group.add(row)
        self._control_widgets[key] = switch
        return switch

    def _spin_row(
        self,
        group: Adw.PreferencesGroup,
        key: str,
        title: str,
        value: int,
        minimum: int,
        maximum: int,
        step: int,
        callback: Callable[[int], None],
        subtitle: str | None = None,
        unit: str = "",
    ) -> Adw.ActionRow:
        row = self._action_row(title, subtitle)
        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        adjustment = Gtk.Adjustment(
            value=value,
            lower=minimum,
            upper=maximum,
            step_increment=step,
            page_increment=max(step, (maximum - minimum) // 10),
        )
        spin = Gtk.SpinButton(
            adjustment=adjustment, digits=0, numeric=True, valign=Gtk.Align.CENTER
        )
        spin.set_width_chars(max(4, len(str(maximum))))
        suffix.append(spin)
        if unit:
            unit_label = Gtk.Label(label=unit)
            unit_label.add_css_class("dim-label")
            suffix.append(unit_label)
        row.add_suffix(suffix)

        def changed(widget: Gtk.SpinButton) -> None:
            if not self._updating:
                callback(widget.get_value_as_int())

        spin.connect("value-changed", changed)
        group.add(row)
        self._control_widgets[key] = spin
        return row

    def _combo_row(
        self,
        group: Adw.PreferencesGroup,
        key: str,
        title: str,
        labels: list[str],
        selected: int,
        callback: Callable[[int], None],
        subtitle: str | None = None,
    ) -> Gtk.DropDown:
        row = self._action_row(title, subtitle)
        dropdown = Gtk.DropDown.new_from_strings(labels)
        dropdown.set_selected(max(0, min(selected, len(labels) - 1)))
        dropdown.set_valign(Gtk.Align.CENTER)

        def changed(widget: Gtk.DropDown, _param: object) -> None:
            if not self._updating:
                callback(widget.get_selected())

        dropdown.connect("notify::selected", changed)
        row.add_suffix(dropdown)
        group.add(row)
        self._control_widgets[key] = dropdown
        return dropdown

    def _sync_control_widgets(self, values: dict[str, Any]) -> None:
        previous_updating = self._updating
        self._updating = True
        try:
            for key, value in values.items():
                widget = self._control_widgets.get(key)
                if isinstance(widget, Gtk.Switch):
                    widget.set_active(bool(value))
                elif isinstance(widget, Gtk.SpinButton) and isinstance(value, int | float):
                    widget.set_value(float(value))
                elif isinstance(widget, Gtk.DropDown):
                    selected = control_dropdown_index(key, value)
                    if selected is not None:
                        widget.set_selected(selected)
            if "focus_auto" in values and hasattr(self, "focus_row"):
                self.focus_row.set_sensitive(not bool(values["focus_auto"]))
            if "white_balance_auto" in values and hasattr(self, "white_balance_row"):
                self.white_balance_row.set_sensitive(not bool(values["white_balance_auto"]))
            if "auto_exposure" in values:
                enabled = bool(values["auto_exposure"])
                if hasattr(self, "iso_row"):
                    self.iso_row.set_sensitive(not enabled)
                if hasattr(self, "shutter_row"):
                    self.shutter_row.set_sensitive(not enabled)
        finally:
            self._updating = previous_updating

    def _build_presets_page(self) -> Gtk.Widget:
        page, content = self._page()
        group = self._group(
            "Scene presets",
            "Store up to 10 combinations of view, smart-mode, and image settings.",
        )
        save_row = self._action_row("Save current scene", "Capture the camera's current settings")
        save_button = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
        save_button.add_css_class("suggested-action")
        save_button.connect("clicked", lambda *_args: self._save_preset_dialog())
        save_row.add_suffix(save_button)
        save_row.set_activatable_widget(save_button)
        group.add(save_row)
        content.append(group)

        self.preset_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.preset_list.add_css_class("boxed-list")
        content.append(self.preset_list)
        self._refresh_preset_list()
        return page

    def _build_view_page(self) -> Gtk.Widget:
        page, content = self._page()
        direction = self._group("Gimbal", "Move in 5° steps using cooperative V4L2 controls.")
        grid_row = self._action_row("Direction")
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, halign=Gtk.Align.CENTER)
        movements = [
            ("go-up-symbolic", 0, 1, 0, 5),
            ("go-previous-symbolic", 1, 0, -5, 0),
            ("find-location-symbolic", 1, 1, None, None),
            ("go-next-symbolic", 1, 2, 5, 0),
            ("go-down-symbolic", 2, 1, 0, -5),
        ]
        for icon, row, column, pan_delta, tilt_delta in movements:
            button = Gtk.Button(icon_name=icon)
            button.add_css_class("circular")
            if pan_delta is None:
                button.set_tooltip_text("Center gimbal and reset zoom")
                button.connect("clicked", lambda *_args: self._center_camera())
            else:
                button.connect(
                    "clicked",
                    lambda _button, pan=pan_delta, tilt=tilt_delta: self._move_camera(pan, tilt),
                )
            grid.attach(button, column, row, 1, 1)
        grid_row.add_suffix(grid)
        direction.add(grid_row)
        self._spin_row(
            direction,
            "zoom",
            "Zoom",
            int(self.state.get("zoom", 100)),
            100,
            400,
            5,
            lambda value: self._set_standard("zoom", value),
            "Digital zoom up to 4×",
            "%",
        )
        content.append(direction)

        orientation = self._group("Orientation")
        self._switch_row(
            orientation,
            "mirror",
            "Horizontal mirror",
            bool(self.state.get("mirror", False)),
            lambda active: self._set_feature("mirror", FEATURE_MIRROR, active),
        )
        self._combo_row(
            orientation,
            "orientation",
            "Software orientation",
            ["Landscape", "Vertical flip", "Portrait right", "Portrait left", "Rotate 180°"],
            0,
            self._orientation_changed,
            "Applied to preview, recordings, screenshots, and the virtual camera",
        )
        self._switch_row(
            orientation,
            "privacy",
            "Privacy mode",
            bool(self.state.get("privacy", False)),
            self._set_privacy,
            "Tilts the Link 2 down and stops its camera stream",
        )
        content.append(orientation)

        focus = self._group("Focus")
        autofocus = bool(self.state.get("focus_auto", True))
        self.focus_auto_switch = self._switch_row(
            focus,
            "focus_auto",
            "Autofocus",
            autofocus,
            self._set_autofocus,
        )
        self.focus_row = self._spin_row(
            focus,
            "focus",
            "Manual focus",
            int(self.state.get("focus", 50)),
            0,
            100,
            1,
            lambda value: self._set_standard("focus", value),
        )
        self.focus_row.set_sensitive(not autofocus)
        content.append(focus)
        return page

    def _build_image_page(self) -> Gtk.Widget:
        page, content = self._page()
        exposure = self._group("Exposure")
        self._switch_row(
            exposure,
            "hdr",
            "HDR",
            bool(self.state.get("hdr", False)),
            lambda active: self._set_feature("hdr", FEATURE_HDR, active),
            "Unavailable at 4K and high-frame-rate modes",
        )
        auto_exposure = bool(self.state.get("auto_exposure", True))
        self.auto_exposure_switch = self._switch_row(
            exposure,
            "auto_exposure",
            "Automatic exposure",
            auto_exposure,
            self._set_auto_exposure,
        )
        self._spin_row(
            exposure,
            "exposure_compensation",
            "Exposure curve",
            int(self.state.get("exposure_compensation", 50)),
            0,
            100,
            1,
            lambda value: self._debounce(
                "exposure_compensation",
                lambda: self.camera.set_exposure_compensation(value),
            ),
            "50 is neutral; range is approximately −3 EV to +3 EV",
        )
        self.iso_row = self._spin_row(
            exposure,
            "iso",
            "Manual ISO",
            int(self.state.get("iso", 100)),
            0,
            65535,
            10,
            lambda value: self._debounce("iso", lambda: self.camera.set_manual_iso(value)),
            "Firmware value; automatic exposure must be off",
        )
        self.shutter_row = self._spin_row(
            exposure,
            "shutter_us",
            "Shutter",
            int(self.state.get("shutter_us", 1000)),
            0,
            65535,
            100,
            lambda value: self._debounce("shutter", lambda: self.camera.set_shutter(value)),
            "Exposure time in microseconds",
            "µs",
        )
        self.iso_row.set_sensitive(not auto_exposure)
        self.shutter_row.set_sensitive(not auto_exposure)
        content.append(exposure)

        color = self._group("White balance and color")
        auto_white = bool(self.state.get("white_balance_auto", True))
        self.white_balance_switch = self._switch_row(
            color,
            "white_balance_auto",
            "Automatic white balance",
            auto_white,
            self._set_auto_white_balance,
        )
        self.white_balance_row = self._spin_row(
            color,
            "white_balance_temperature",
            "Color temperature",
            int(self.state.get("white_balance_temperature", 6400)),
            2000,
            10000,
            100,
            lambda value: self._set_standard("white_balance_temperature", value),
            unit="K",
        )
        self.white_balance_row.set_sensitive(not auto_white)
        for key in ("brightness", "contrast", "saturation", "hue", "sharpness"):
            spec = STANDARD_CONTROLS[key]
            self._spin_row(
                color,
                key,
                spec.label,
                int(self.state.get(key, spec.default)),
                spec.minimum,
                spec.maximum,
                spec.step,
                lambda value, control=key: self._set_standard(control, value),
            )
        anti_flicker = int(self.state.get("anti_flicker", 2))
        self._combo_row(
            color,
            "anti_flicker",
            "Anti-flicker",
            list(ANTI_FLICKER_LABELS),
            max(0, min(anti_flicker, len(ANTI_FLICKER_LABELS) - 1)),
            lambda selected: self._set_standard("anti_flicker", selected),
        )
        content.append(color)

        templates = self._group(
            "Color templates", "Save and recall image-only settings independently of scenes."
        )
        save_color_row = self._action_row("Save current colors")
        save_color = Gtk.Button(label="Save", valign=Gtk.Align.CENTER)
        save_color.add_css_class("suggested-action")
        save_color.connect("clicked", lambda *_args: self._save_color_preset_dialog())
        save_color_row.add_suffix(save_color)
        templates.add(save_color_row)
        content.append(templates)
        self.color_preset_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.color_preset_list.add_css_class("boxed-list")
        content.append(self.color_preset_list)
        self._refresh_color_preset_list()
        return page

    def _build_smart_page(self) -> Gtk.Widget:
        page, content = self._page()
        tracking = self._group(
            "AI Tracking",
            "Select AI Tracking from the mode bar beneath the preview before adjusting framing.",
        )
        framing_names = list(FRAMING_MODES)
        current_framing = str(self.state.get("framing", "head"))
        framing_index = (
            framing_names.index(current_framing) if current_framing in framing_names else 0
        )
        self._combo_row(
            tracking,
            "framing",
            "Smart composition",
            ["Head", "Half body", "Whole body"],
            framing_index,
            lambda selected: self._submit(
                "Smart composition updated",
                lambda: self.camera.set_framing(framing_names[selected]),
            ),
        )
        self._spin_row(
            tracking,
            "tracking_speed",
            "Tracking speed",
            int(self.state.get("tracking_speed", 1)),
            0,
            TRACKING_SPEED_MAX,
            1,
            lambda value: self._debounce(
                "tracking_speed", lambda: self.camera.set_tracking_speed(value)
            ),
            "Full firmware range",
        )
        self._combo_row(
            tracking,
            "tracking_engine",
            "Tracking engine",
            ["Camera AI (single)", "Link Studio single + areas", "Link Studio group + areas"],
            0,
            self._tracking_engine_changed,
            "Local modes use on-device face landmarks and support tracking/pause areas",
        )
        content.append(tracking)

        regions = self._group(
            "Tracking areas",
            "Start an editor, then drag a rectangle over the live preview. "
            "Up to six pause areas are supported.",
        )
        region_row = self._action_row("Tracking boundary")
        draw_tracking = Gtk.Button(label="Draw", valign=Gtk.Align.CENTER)
        draw_tracking.connect("clicked", lambda *_args: self._begin_region_edit("tracking"))
        clear_tracking = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        clear_tracking.add_css_class("flat")
        clear_tracking.connect("clicked", lambda *_args: self._clear_tracking_area())
        region_row.add_suffix(draw_tracking)
        region_row.add_suffix(clear_tracking)
        regions.add(region_row)
        pause_row = self._action_row(
            "Pause-track areas", "Tracking motion pauses inside these zones"
        )
        draw_pause = Gtk.Button(label="Add", valign=Gtk.Align.CENTER)
        draw_pause.connect("clicked", lambda *_args: self._begin_region_edit("pause"))
        clear_pause = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear_pause.add_css_class("flat")
        clear_pause.connect("clicked", lambda *_args: self._clear_pause_areas())
        pause_row.add_suffix(draw_pause)
        pause_row.add_suffix(clear_pause)
        regions.add(pause_row)
        content.append(regions)

        gestures = self._group("Gesture control")
        self._switch_row(
            gestures,
            "gesture_zoom",
            "Gesture zoom",
            bool(self.state.get("gesture_zoom", False)),
            lambda active: self._set_feature("gesture_zoom", FEATURE_GESTURE_ZOOM, active),
            "Use an L-shaped hand gesture to zoom",
        )
        note = self._action_row(
            "Tracking and whiteboard gestures",
            "The current firmware exposes these gestures as part of their AI modes; "
            "separate USB bits are still being mapped.",
            "dialog-information-symbolic",
        )
        gestures.add(note)
        content.append(gestures)
        return page

    def _build_effects_page(self) -> Gtk.Widget:
        page, content = self._page()
        filters = self._group(
            "Live filter", "Applied to Link Studio preview, screenshots, and recordings."
        )
        self._combo_row(
            filters,
            "filter",
            "Filter",
            ["None", "Monochrome", "Punch", "Soft"],
            0,
            lambda selected: self.preview.set_filter(["none", "mono", "punch", "soft"][selected]),
        )
        content.append(filters)

        advanced = self._group(
            "Local AI effects",
            "Person segmentation and face landmarks run entirely on this computer.",
        )
        self._combo_row(
            advanced,
            "effect_mode",
            "Effect",
            [
                "None",
                "Background blur",
                "Natural bokeh",
                "Background replacement",
                "Green screen keying",
                "Beautify",
                "Makeup",
                "Smart Whiteboard",
            ],
            0,
            self._effect_mode_changed,
        )
        self._spin_row(
            advanced,
            "effect_intensity",
            "Intensity",
            55,
            0,
            100,
            1,
            lambda value: self.preview.set_effects(intensity=value),
            unit="%",
        )

        background_row = self._action_row("Background color")
        background_rgba = Gdk.RGBA()
        background_rgba.parse("#242424")
        self.background_color_button = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(title="Choose background color"), rgba=background_rgba
        )
        self.background_color_button.connect(
            "notify::rgba", lambda button, _param: self._background_color_changed(button)
        )
        background_row.add_suffix(self.background_color_button)
        advanced.add(background_row)

        image_row = self._action_row("Background image", "Center-cropped to fill the output")
        self.background_image_label = Gtk.Label(label="None")
        self.background_image_label.add_css_class("dim-label")
        choose_image = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose_image.connect("clicked", lambda *_args: self._choose_background_image())
        clear_image = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        clear_image.add_css_class("flat")
        clear_image.connect("clicked", lambda *_args: self._clear_background_image())
        image_row.add_suffix(self.background_image_label)
        image_row.add_suffix(choose_image)
        image_row.add_suffix(clear_image)
        advanced.add(image_row)

        key_row = self._action_row("Green-screen key color")
        key_rgba = Gdk.RGBA()
        key_rgba.parse("#00ff00")
        self.key_color_button = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(title="Choose key color"), rgba=key_rgba
        )
        self.key_color_button.connect(
            "notify::rgba", lambda button, _param: self._key_color_changed(button)
        )
        key_row.add_suffix(self.key_color_button)
        advanced.add(key_row)
        self._spin_row(
            advanced,
            "key_tolerance",
            "Key tolerance",
            70,
            1,
            255,
            1,
            lambda value: self.preview.set_effects(key_tolerance=value),
        )
        content.append(advanced)

        virtual = self._group("Virtual camera")
        virtual_subtitle = (
            f"Publish the processed feed to {self.virtual_devices[0]}"
            if self.virtual_devices
            else "Run link-studio-setup-virtual-camera, then restart Link Studio"
        )
        self.virtual_camera_switch = self._switch_row(
            virtual,
            "virtual_camera",
            "Link Studio Virtual Camera",
            False,
            self._set_virtual_camera,
            virtual_subtitle,
        )
        self.virtual_camera_switch.set_sensitive(bool(self.virtual_devices))
        content.append(virtual)
        return page

    def _build_audio_page(self) -> Gtk.Widget:
        page, content = self._page()
        microphone = self._group("Link 2 microphone")
        source_labels = [source.description for source in self.audio_sources] or ["No input found"]
        selected_source = self.selected_audio_source
        selected_index = (
            self.audio_sources.index(selected_source)
            if selected_source in self.audio_sources
            else 0
        )
        source_dropdown = self._combo_row(
            microphone,
            "audio_source",
            "Audio source",
            source_labels,
            selected_index,
            self._audio_source_changed,
        )
        source_dropdown.set_sensitive(bool(self.audio_sources))
        audio_modes = ["voice_focus", "voice_suppression", "music_balance"]
        current_audio_mode = str(self.state.get("audio_mode", "voice_focus"))
        self._combo_row(
            microphone,
            "audio_mode",
            "Audio mode",
            ["Voice Focus", "Voice Suppression", "Music Balance"],
            audio_modes.index(current_audio_mode) if current_audio_mode in audio_modes else 0,
            lambda selected: self._submit(
                f"Audio mode set to {audio_modes[selected].replace('_', ' ').title()}",
                lambda: self.camera.set_audio_mode(audio_modes[selected]),
            ),
            "Link 2 microphone processing; Music Balance preserves music and ambient sound",
        )
        self.audio_mute_switch = self._switch_row(
            microphone,
            "audio_mute",
            "Mute microphone",
            selected_source.muted if selected_source else False,
            self._set_audio_mute,
        )
        self.audio_volume_row = self._spin_row(
            microphone,
            "audio_volume",
            "Input volume",
            selected_source.volume_percent if selected_source else 100,
            0,
            150,
            1,
            self._set_audio_volume,
            "PipeWire source volume",
            "%",
        )
        self.audio_mute_switch.set_sensitive(selected_source is not None)
        self.audio_volume_row.set_sensitive(selected_source is not None)
        if selected_source:
            details = self._action_row(
                "Capture format",
                f"{selected_source.channels} channel · "
                f"{selected_source.sample_rate // 1000} kHz · AAC in recordings",
                "audio-input-microphone-symbolic",
            )
            microphone.add(details)
        content.append(microphone)
        return page

    def _build_tools_page(self) -> Gtk.Widget:
        page, content = self._page()

        teleprompter = self._group(
            "Teleprompter",
            "Create or import scripts, then present them in an adjustable auto-scrolling window.",
        )
        new_script_row = self._action_row(
            "Scripts", "Up to 100 scripts and 100,000 characters per script"
        )
        import_script = Gtk.Button(label="Import…", valign=Gtk.Align.CENTER)
        import_script.connect("clicked", lambda *_args: self._import_script())
        create_script = Gtk.Button(label="Create", valign=Gtk.Align.CENTER)
        create_script.add_css_class("suggested-action")
        create_script.connect("clicked", lambda *_args: self._script_dialog())
        new_script_row.add_suffix(import_script)
        new_script_row.add_suffix(create_script)
        teleprompter.add(new_script_row)
        content.append(teleprompter)
        self.script_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.script_list.add_css_class("boxed-list")
        content.append(self.script_list)
        self._refresh_script_list()

        recording = self._group(
            "Local AI recording",
            "Records the selected microphone locally. Optional Whisper transcription "
            "and summaries stay on this computer.",
        )
        self.ai_record_toggle = Gtk.ToggleButton(
            label="Start AI Recording", valign=Gtk.Align.CENTER
        )
        self.ai_record_toggle.connect("toggled", self._ai_recording_toggled)
        record_row = self._action_row(
            "Voice notes", "WAV audio, timestamped markers, transcript, and Markdown summary"
        )
        record_row.add_suffix(self.ai_record_toggle)
        recording.add(record_row)

        self.ai_record_status = Gtk.Label(label="Ready", xalign=0, selectable=True)
        self.ai_record_status.add_css_class("dim-label")
        status_row = self._action_row("Status")
        status_row.add_suffix(self.ai_record_status)
        recording.add(status_row)

        marker_row = self._action_row("Recording marker", "Add a timestamped highlight or note")
        self.ai_marker_entry = Gtk.Entry(placeholder_text="Optional note")
        self.ai_marker_entry.set_width_chars(18)
        self.ai_marker_entry.set_sensitive(False)
        marker_button = Gtk.Button(label="Mark", valign=Gtk.Align.CENTER, sensitive=False)
        marker_button.connect("clicked", lambda *_args: self._add_ai_marker())
        self.ai_marker_button = marker_button
        marker_row.add_suffix(self.ai_marker_entry)
        marker_row.add_suffix(marker_button)
        recording.add(marker_row)

        action_row = self._action_row("Recording controls")
        self.ai_pause_button = Gtk.ToggleButton(
            label="Pause", valign=Gtk.Align.CENTER, sensitive=False
        )
        self.ai_pause_button.connect("toggled", self._ai_pause_toggled)
        self.ai_transcribe_button = Gtk.Button(
            label="Transcribe", valign=Gtk.Align.CENTER, sensitive=False
        )
        self.ai_transcribe_button.connect("clicked", lambda *_args: self._transcribe_last_meeting())
        open_recordings = Gtk.Button(label="View", valign=Gtk.Align.CENTER)
        open_recordings.connect("clicked", lambda *_args: self._open_meeting_folder())
        action_row.add_suffix(self.ai_pause_button)
        action_row.add_suffix(self.ai_transcribe_button)
        action_row.add_suffix(open_recordings)
        recording.add(action_row)

        local_ai_subtitle = (
            f"Whisper ready · model expected at {default_whisper_model()}"
            if whisper_available()
            else "Run link-studio-setup-local-ai once to install offline Whisper"
        )
        setup_row = self._action_row("Offline transcription", local_ai_subtitle)
        setup_button = Gtk.Button(label="Set Up", valign=Gtk.Align.CENTER)
        setup_button.connect("clicked", lambda *_args: self._setup_local_ai())
        setup_row.add_suffix(setup_button)
        recording.add(setup_row)
        content.append(recording)
        return page

    def _build_device_page(self) -> Gtk.Widget:
        page, content = self._page()
        information = self._group("Device information")
        device_information = self.state.get("device_information", {})
        if not isinstance(device_information, dict):
            device_information = {}
        for title, value in (
            ("Model", self.camera.device.model),
            (
                "USB device",
                f"{self.camera.device.vendor_id:04x}:{self.camera.device.product_id:04x}",
            ),
            ("Firmware", str(device_information.get("firmware_version", "Unknown"))),
            ("Firmware build", str(device_information.get("firmware_build", "Unknown"))),
        ):
            row = self._action_row(title)
            label = Gtk.Label(label=value, selectable=True)
            label.add_css_class("dim-label")
            row.add_suffix(label)
            information.add(row)
        firmware_row = self._action_row(
            "Firmware update",
            "Uses Insta360's recovery-safe U-Disk procedure; the current version is "
            "read directly from the camera.",
        )
        firmware_link = Gtk.LinkButton(
            uri="https://www.insta360.com/download/insta360-link",
            label="Download / instructions",
            valign=Gtk.Align.CENTER,
        )
        firmware_row.add_suffix(firmware_link)
        information.add(firmware_row)
        content.append(information)

        remote = self._group(
            "Phone remote",
            "Starts a token-authenticated controller on this LAN only. "
            "No account or cloud is used.",
        )
        self.remote_switch = self._switch_row(
            remote,
            "phone_remote",
            "Enable phone remote",
            False,
            self._set_remote,
        )
        self.remote_url_row = self._action_row(
            "Pairing address", "Scan the QR code or open this URL"
        )
        self.remote_url_label = Gtk.Label(label="Remote is off", selectable=True, wrap=True)
        self.remote_url_label.set_max_width_chars(32)
        self.remote_url_label.add_css_class("dim-label")
        self.remote_url_row.add_suffix(self.remote_url_label)
        remote.add(self.remote_url_row)
        self.remote_qr = Gtk.Picture(can_shrink=True, width_request=220, height_request=220)
        self.remote_qr.set_visible(False)
        remote.add(self.remote_qr)
        content.append(remote)

        application = self._group("Application")
        compact_row = self._action_row("Compact toolbar", "Also available with Ctrl+M")
        compact_switch = Gtk.Switch(active=False, valign=Gtk.Align.CENTER)
        compact_switch.bind_property(
            "active", self.compact_button, "active", GObject.BindingFlags.BIDIRECTIONAL
        )
        compact_row.add_suffix(compact_switch)
        application.add(compact_row)
        shortcut_row = self._action_row(
            "Global shortcuts",
            "Compositor-managed shortcuts work while Link Studio is in the background.",
        )
        shortcut_enabled = bool(
            getattr(getattr(self.get_application(), "global_shortcuts", None), "settings", None)
            and self.get_application().global_shortcuts.settings.enabled
        )
        self.global_shortcut_switch = Gtk.Switch(active=shortcut_enabled, valign=Gtk.Align.CENTER)
        self.global_shortcut_switch.connect(
            "notify::active",
            lambda switch, _param: self._set_global_shortcuts(switch.get_active()),
        )
        configure_shortcuts = Gtk.Button(label="Configure", valign=Gtk.Align.CENTER)
        configure_shortcuts.add_css_class("flat")
        configure_shortcuts.connect("clicked", lambda *_args: self._configure_global_shortcuts())
        shortcut_row.add_suffix(configure_shortcuts)
        shortcut_row.add_suffix(self.global_shortcut_switch)
        application.add(shortcut_row)
        self.global_shortcut_status = Gtk.Label(
            label="Portal shortcuts active" if shortcut_enabled else "Off",
            xalign=0,
            wrap=True,
        )
        self.global_shortcut_status.add_css_class("dim-label")
        shortcut_status_row = self._action_row("Shortcut status")
        shortcut_status_row.add_suffix(self.global_shortcut_status)
        application.add(shortcut_status_row)
        screenshot_location = self._action_row(
            "Screenshot location",
            str(self.storage_settings.screenshot_directory or (_pictures_dir() / "Link Studio")),
        )
        choose_screenshots = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose_screenshots.connect(
            "clicked", lambda *_args: self._choose_output_folder("screenshot", screenshot_location)
        )
        screenshot_location.add_suffix(choose_screenshots)
        application.add(screenshot_location)
        recording_location = self._action_row(
            "Recording location",
            str(self.storage_settings.recording_directory or (_videos_dir() / "Link Studio")),
        )
        choose_recordings = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        choose_recordings.connect(
            "clicked", lambda *_args: self._choose_output_folder("recording", recording_location)
        )
        recording_location.add_suffix(choose_recordings)
        application.add(recording_location)
        support_row = self._action_row(
            "Support bundle",
            "Exports camera, video/audio stack, format, and rotating-log diagnostics "
            "without camera images.",
        )
        support_button = Gtk.Button(label="Export", valign=Gtk.Align.CENTER)
        support_button.connect("clicked", lambda *_args: self.export_support_bundle())
        support_row.add_suffix(support_button)
        application.add(support_row)
        content.append(application)
        return page

    def _choose_output_folder(self, kind: str, row: Adw.ActionRow) -> None:
        title = "Choose screenshot folder" if kind == "screenshot" else "Choose recording folder"
        dialog = Gtk.FileDialog(title=title, modal=True)

        def selected(file_dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = file_dialog.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path()
            if not path:
                self._toast("Only local folders are supported")
                return
            directory = Path(path)
            self.storage_settings.set_directory(kind, directory)
            row.set_subtitle(str(directory))
            self._toast(f"{kind.title()} location updated")

        dialog.select_folder(self, None, selected)

    def _set_global_shortcuts(self, enabled: bool) -> None:
        if self._updating:
            return
        application = self.get_application()
        if application and hasattr(application, "set_global_shortcuts"):
            application.set_global_shortcuts(enabled)

    def _configure_global_shortcuts(self) -> None:
        application = self.get_application()
        if application and hasattr(application, "configure_global_shortcuts"):
            application.configure_global_shortcuts()

    def global_shortcut_status_changed(self, message: str, enabled: bool) -> None:
        self.global_shortcut_status.set_label(message)
        if self.global_shortcut_switch.get_active() != enabled:
            self._updating = True
            self.global_shortcut_switch.set_active(enabled)
            self._updating = False

    def activate_global_shortcut(self, identifier: str) -> None:
        if identifier == "preview":
            self.preview_toggle.set_active(not self.preview_toggle.get_active())
        elif identifier == "record":
            self.record_button.set_active(not self.record_button.get_active())
        elif identifier == "screenshot":
            self.take_screenshot()
        elif identifier == "compact":
            self.compact_button.set_active(not self.compact_button.get_active())
        elif identifier in {"tracking", "whiteboard"}:
            target = identifier
            current = str(self.state.get("mode", "normal"))
            self._mode_buttons[target if current != target else "normal"].set_active(True)
        elif identifier.startswith("gimbal_"):
            deltas = {
                "gimbal_up": (0, 5),
                "gimbal_down": (0, -5),
                "gimbal_left": (-5, 0),
                "gimbal_right": (5, 0),
            }
            self._move_camera(*deltas[identifier])
        elif identifier == "center":
            self._center_camera()
        elif identifier in {"zoom_in", "zoom_out"}:
            widget = self._control_widgets.get("zoom")
            if isinstance(widget, Gtk.SpinButton):
                delta = 5 if identifier == "zoom_in" else -5
                widget.set_value(max(100, min(400, widget.get_value_as_int() + delta)))
        elif identifier == "privacy":
            widget = self._control_widgets.get("privacy")
            if isinstance(widget, Gtk.Switch):
                widget.set_active(not widget.get_active())

    # ----------------------------------------------------- tools / meetings

    def _refresh_script_list(self) -> None:
        child = self.script_list.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.script_list.remove(child)
            child = following
        if not self.script_store.scripts:
            self.script_list.append(
                Adw.ActionRow(
                    title="No teleprompter scripts",
                    subtitle="Create a script or import a UTF-8 text file.",
                )
            )
            return
        for index, script in enumerate(self.script_store.scripts):
            row = Adw.ActionRow(
                title=script.name,
                subtitle=f"{len(script.text):,} characters",
            )
            present = Gtk.Button(label="Present", valign=Gtk.Align.CENTER)
            present.connect(
                "clicked", lambda _button, selected=index: self._open_teleprompter(selected)
            )
            edit = Gtk.Button(
                icon_name="document-edit-symbolic",
                tooltip_text="Edit script",
                valign=Gtk.Align.CENTER,
            )
            edit.add_css_class("flat")
            edit.connect("clicked", lambda _button, selected=index: self._script_dialog(selected))
            delete = Gtk.Button(
                icon_name="user-trash-symbolic",
                tooltip_text="Delete script",
                valign=Gtk.Align.CENTER,
            )
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _button, selected=index: self._delete_script(selected))
            row.add_suffix(present)
            row.add_suffix(edit)
            row.add_suffix(delete)
            self.script_list.append(row)

    def _script_dialog(self, index: int | None = None) -> None:
        existing = self.script_store.scripts[index] if index is not None else None
        dialog = Adw.AlertDialog(
            heading="Edit teleprompter script" if existing else "Create teleprompter script",
            body="Scripts are stored locally and limited to 100,000 characters.",
        )
        editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        name = Gtk.Entry(placeholder_text="Script name")
        name.set_text(
            existing.name if existing else f"Untitled {len(self.script_store.scripts) + 1}"
        )
        editor.append(name)
        text_scroll = Gtk.ScrolledWindow(min_content_height=280, min_content_width=520)
        script_text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        script_text.get_buffer().set_text(existing.text if existing else "")
        text_scroll.set_child(script_text)
        editor.append(text_scroll)
        dialog.set_extra_child(editor)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def response(_dialog: Adw.AlertDialog, response_id: str) -> None:
            if response_id != "save":
                return
            buffer = script_text.get_buffer()
            text_value = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            try:
                if index is None:
                    self.script_store.add(name.get_text(), text_value)
                else:
                    self.script_store.update(index, name.get_text(), text_value)
            except ValueError as exc:
                self._toast(str(exc))
                return
            self._refresh_script_list()
            self._toast("Teleprompter script saved")

        dialog.connect("response", response)
        dialog.present(self)

    def _import_script(self) -> None:
        dialog = Gtk.FileDialog(title="Import teleprompter script", modal=True)
        text_filter = Gtk.FileFilter(name="Text files")
        text_filter.add_mime_type("text/plain")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(text_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self._script_imported)

    def _script_imported(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            selected = dialog.open_finish(result)
        except GLib.Error:
            return
        path_value = selected.get_path()
        if not path_value:
            self._toast("Only local text files can be imported")
            return
        path = Path(path_value)
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                text_value = handle.read(self.script_store.MAX_CHARACTERS + 1)
            self.script_store.add(path.stem, text_value)
        except (OSError, ValueError) as exc:
            self._toast(f"Script import failed: {exc}")
            return
        self._refresh_script_list()
        if len(text_value) > self.script_store.MAX_CHARACTERS:
            self._toast("Script imported and truncated to 100,000 characters")
        else:
            self._toast(f"Imported “{path.name}”")

    def _open_teleprompter(self, index: int) -> None:
        application = self.get_application()
        if not isinstance(application, Gtk.Application):
            return
        window = TeleprompterWindow(application, self.script_store.scripts[index])
        window.set_transient_for(self)
        self._teleprompter_windows.append(window)

        def closed(*_args: object) -> bool:
            if window in self._teleprompter_windows:
                self._teleprompter_windows.remove(window)
            return False

        window.connect("close-request", closed)
        window.present()

    def _delete_script(self, index: int) -> None:
        name = self.script_store.scripts[index].name
        self.script_store.remove(index)
        self._refresh_script_list()
        self._toast(f"Deleted script “{name}”")

    def _ai_recording_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._updating:
            return
        if button.get_active():
            self.ai_recorder.audio_source = (
                self.selected_audio_source.name if self.selected_audio_source else None
            )
            try:
                directory = self.ai_recorder.start()
            except Exception as exc:
                self._toast(f"AI recording failed: {exc}")
                self._updating = True
                button.set_active(False)
                self._updating = False
                return
            button.set_label("Stop Recording")
            button.add_css_class("destructive-action")
            self.ai_record_status.set_label(f"Recording · 0:00 · {directory.name}")
            self.ai_pause_button.set_sensitive(True)
            self.ai_marker_entry.set_sensitive(True)
            self.ai_marker_button.set_sensitive(True)
            self.ai_transcribe_button.set_sensitive(False)
            if not self._ai_recording_timer:
                self._ai_recording_timer = GLib.timeout_add_seconds(
                    1, self._update_ai_recording_status
                )
            return

        button.set_sensitive(False)
        button.set_label("Finalizing…")
        self.ai_pause_button.set_sensitive(False)
        self.ai_marker_entry.set_sensitive(False)
        self.ai_marker_button.set_sensitive(False)
        if self._ai_recording_timer:
            GLib.source_remove(self._ai_recording_timer)
            self._ai_recording_timer = 0

        def finished(result: MeetingResult) -> None:
            self.last_meeting_result = result
            duration = self._format_duration(result.duration_seconds)
            self.ai_record_status.set_label(f"Saved · {duration} · {result.directory.name}")
            button.set_label("Start AI Recording")
            button.remove_css_class("destructive-action")
            button.set_sensitive(True)
            self.ai_transcribe_button.set_sensitive(True)

        self._submit("", self.ai_recorder.stop, on_success=finished, quiet=True)

    def _update_ai_recording_status(self) -> bool:
        if not self.ai_recorder.running:
            self._ai_recording_timer = 0
            return False
        state = "Paused" if self.ai_recorder.paused else "Recording"
        self.ai_record_status.set_label(
            f"{state} · {self._format_duration(self.ai_recorder.elapsed)}"
        )
        return True

    @staticmethod
    def _format_duration(seconds: float) -> str:
        minutes, remaining = divmod(round(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{remaining:02d}" if hours else f"{minutes}:{remaining:02d}"

    def _ai_pause_toggled(self, button: Gtk.ToggleButton) -> None:
        if not self.ai_recorder.running:
            return
        try:
            paused = self.ai_recorder.set_paused(button.get_active())
        except Exception as exc:
            self._toast(f"Recording control failed: {exc}")
            return
        button.set_label("Resume" if paused else "Pause")
        self._update_ai_recording_status()

    def _add_ai_marker(self) -> None:
        note = self.ai_marker_entry.get_text()
        try:
            marker = self.ai_recorder.add_marker("note" if note.strip() else "highlight", note)
        except Exception as exc:
            self._toast(str(exc))
            return
        self.ai_marker_entry.set_text("")
        self._toast(f"Marker added at {self._format_duration(marker.seconds)}")

    def _transcribe_last_meeting(self) -> None:
        result = self.last_meeting_result
        if result is None:
            self._toast("Complete a local AI recording first")
            return
        self.ai_transcribe_button.set_sensitive(False)
        self.ai_record_status.set_label("Transcribing locally…")

        def finished(paths: tuple[Path, Path]) -> None:
            transcript, summary = paths
            self.ai_record_status.set_label(f"Summary ready · {summary.parent.name}")
            self.ai_transcribe_button.set_sensitive(True)
            self._toast(f"Transcript saved to {transcript}")

        def failed(_error: Exception) -> None:
            self.ai_transcribe_button.set_sensitive(True)
            self.ai_record_status.set_label("Transcription unavailable")

        self._submit(
            "",
            lambda: transcribe_meeting(result),
            on_success=finished,
            on_error=failed,
            quiet=True,
        )

    def _open_meeting_folder(self) -> None:
        directory = default_meeting_dir()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            Gio.AppInfo.launch_default_for_uri(directory.as_uri(), None)
        except GLib.Error as exc:
            self._toast(f"Could not open recordings: {exc}")

    def _setup_local_ai(self) -> None:
        helper = GLib.find_program_in_path("link-studio-setup-local-ai")
        if not helper:
            project_helper = Path(__file__).resolve().parents[2] / "scripts/setup-local-ai"
            helper = str(project_helper) if project_helper.is_file() else None
        terminal = GLib.find_program_in_path("xdg-terminal-exec")
        if not helper or not terminal:
            self._toast("Run scripts/setup-local-ai from the Link Studio project")
            return
        try:
            Gio.Subprocess.new([terminal, helper], Gio.SubprocessFlags.NONE)
        except GLib.Error as exc:
            self._toast(f"Could not open local AI setup: {exc}")

    # ----------------------------------------------------- compact / effects

    def _compact_toggled(self, button: Gtk.ToggleButton) -> None:
        compact = button.get_active()
        self.main_stack.set_visible_child_name("compact" if compact else "full")
        button.set_icon_name("view-fullscreen-symbolic" if compact else "view-restore-symbolic")
        self.set_default_size(760 if compact else 1280, 150 if compact else 800)
        if compact:
            self.compact_status.set_label(
                self.preview.output_label if self.preview.running else "Ready"
            )

    def _compact_mode_toggled(self, button: Gtk.ToggleButton, mode: str) -> None:
        if self._updating or not button.get_active():
            return
        primary = self._mode_buttons.get(mode)
        if primary and not primary.get_active():
            primary.set_active(True)

    def _sync_mode_buttons(self, mode: str) -> None:
        self._updating = True
        for collection in (self._mode_buttons, self._compact_mode_buttons):
            if mode in collection:
                collection[mode].set_active(True)
        self._updating = False

    @staticmethod
    def _rgba_hex(rgba: Gdk.RGBA) -> str:
        return (
            f"#{round(rgba.red * 255):02x}{round(rgba.green * 255):02x}{round(rgba.blue * 255):02x}"
        )

    def _effect_mode_changed(self, selected: int) -> None:
        modes = [
            "none",
            "background_blur",
            "bokeh",
            "background_replace",
            "green_screen",
            "beauty",
            "makeup",
            "smart_whiteboard",
        ]
        mode = modes[min(selected, len(modes) - 1)]
        self.preview.processor.reset_analysis()
        self.preview.set_effects(mode=mode)
        if mode != "none" and not self.preview_toggle.get_active():
            self.preview_toggle.set_active(True)
        self._toast(
            f"{mode.replace('_', ' ').title()} {'enabled' if mode != 'none' else 'disabled'}"
        )

    def _background_color_changed(self, button: Gtk.ColorDialogButton) -> None:
        self.preview.set_effects(background_color=self._rgba_hex(button.get_rgba()))

    def _key_color_changed(self, button: Gtk.ColorDialogButton) -> None:
        self.preview.set_effects(key_color=self._rgba_hex(button.get_rgba()))

    def _choose_background_image(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a background image", modal=True)
        image_filter = Gtk.FileFilter(name="Images")
        for mime_type in ("image/png", "image/jpeg", "image/webp", "image/bmp"):
            image_filter.add_mime_type(mime_type)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self._background_image_chosen)

    def _background_image_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            selected = dialog.open_finish(result)
        except GLib.Error:
            return
        path = selected.get_path()
        if not path:
            self._toast("Only local image files can be used as a background")
            return
        self.background_image_label.set_label(Path(path).name)
        self.preview.set_effects(background_image=path)

    def _clear_background_image(self) -> None:
        self.background_image_label.set_label("None")
        self.preview.set_effects(background_image=None)

    def _orientation_changed(self, selected: int) -> None:
        orientations = [
            "identity",
            "vertical_flip",
            "rotate_right",
            "rotate_left",
            "rotate_180",
        ]
        if self.record_button.get_active():
            self.record_button.set_active(False)
        if self.virtual_camera:
            self.virtual_camera_switch.set_active(False)
        self.preview.set_effects(orientation=orientations[min(selected, len(orientations) - 1)])
        if self.preview.running:
            self.preview_status.set_label(self.preview.output_label)
            self.compact_status.set_label(self.preview.output_label)

    # ----------------------------------------------------------- area editor

    def _begin_region_edit(self, mode: str) -> None:
        if mode == "pause" and len(self._pause_areas) >= 6:
            self._toast("The six pause-area limit has been reached")
            return
        if not self.preview_toggle.get_active():
            self.preview_toggle.set_active(True)
        self._region_edit_mode = mode
        self._drag_region = None
        self._toast(
            "Drag the tracking boundary over the preview"
            if mode == "tracking"
            else "Drag a pause-track area over the preview"
        )

    def _region_drag_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if self._region_edit_mode:
            self._drag_start = (x, y)

    def _region_drag_update(
        self, _gesture: Gtk.GestureDrag, offset_x: float, offset_y: float
    ) -> None:
        if not self._region_edit_mode or self._drag_start is None:
            return
        width, height = self.preview_overlay.get_width(), self.preview_overlay.get_height()
        if width <= 0 or height <= 0:
            return
        start_x, start_y = self._drag_start
        end_x, end_y = start_x + offset_x, start_y + offset_y
        frame_width, frame_height = (
            (self.latest_frame.width, self.latest_frame.height)
            if self.latest_frame
            else self.preview.output_dimensions
        )
        self._drag_region = frame_region_from_drag(
            (start_x, start_y),
            (end_x, end_y),
            (width, height),
            (frame_width, frame_height),
        )
        self.region_overlay.queue_draw()

    def _region_drag_end(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        region = self._drag_region
        mode = self._region_edit_mode
        self._drag_start = None
        self._drag_region = None
        self._region_edit_mode = None
        if not region or region[2] < 0.02 or region[3] < 0.02:
            self.region_overlay.queue_draw()
            return
        if mode == "tracking":
            self._tracking_area = region
            self._toast("Tracking boundary saved")
        elif mode == "pause":
            self._pause_areas.append(region)
            self._toast(f"Pause-track area {len(self._pause_areas)} saved")
        self._commit_regions()

    def _clear_tracking_area(self) -> None:
        self._tracking_area = (0.0, 0.0, 1.0, 1.0)
        self._commit_regions()
        self._toast("Tracking boundary reset")

    def _clear_pause_areas(self) -> None:
        self._pause_areas.clear()
        self._commit_regions()
        self._toast("Pause-track areas cleared")

    def _commit_regions(self) -> None:
        self.preview.set_effects(
            tracking_area=self._tracking_area, pause_areas=tuple(self._pause_areas)
        )
        self.region_overlay.queue_draw()

    def _draw_regions(self, _area: Gtk.DrawingArea, context: Any, width: int, height: int) -> None:
        frame_width, frame_height = (
            (self.latest_frame.width, self.latest_frame.height)
            if self.latest_frame
            else self.preview.output_dimensions
        )
        content_x, content_y, content_width, content_height = contained_rect(
            width, height, frame_width, frame_height
        )

        def rectangle(region: Rect, red: float, green: float, blue: float) -> None:
            x, y, region_width, region_height = region
            context.rectangle(
                content_x + x * content_width,
                content_y + y * content_height,
                region_width * content_width,
                region_height * content_height,
            )
            context.set_source_rgba(red, green, blue, 0.92)
            context.set_line_width(2.5)
            context.stroke()

        if self._tracking_area != (0.0, 0.0, 1.0, 1.0):
            rectangle(self._tracking_area, 0.25, 0.9, 0.55)
        for region in self._pause_areas:
            rectangle(region, 0.95, 0.3, 0.38)
        if self._drag_region:
            rectangle(self._drag_region, 0.45, 0.7, 1.0)

    # --------------------------------------------------------- local tracking

    def _tracking_engine_changed(self, selected: int) -> None:
        if self._updating:
            return
        mode = ("off", "single", "group")[min(selected, 2)]
        self.preview.set_effects(tracking_mode=mode)
        if mode == "off":
            return
        if not self.preview_toggle.get_active():
            self.preview_toggle.set_active(True)

        def success(_result: Any) -> None:
            self.state["mode"] = "normal"
            self._sync_mode_buttons("normal")

        self._submit(
            f"Local {mode} tracking enabled",
            lambda: self.camera.set_video_mode("normal", verify_streaming=False),
            on_success=success,
        )

    def _tracking_target_received(self, target: TrackingTarget | None) -> None:
        GLib.idle_add(self._apply_tracking_target, target)

    def _apply_tracking_target(self, target: TrackingTarget | None) -> bool:
        if self._closed or target is None or target.paused:
            return False
        now = time.monotonic()
        if now - self._last_tracking_move < 0.22:
            return False
        self._last_tracking_move = now
        error_x = target.center_x - 0.5
        error_y = 0.5 - target.center_y
        pan_delta = 5 if error_x > 0.075 else -5 if error_x < -0.075 else 0
        tilt_delta = 5 if error_y > 0.09 else -5 if error_y < -0.09 else 0
        current_zoom = int(self.state.get("zoom", 100))
        desired_size = 0.43 if target.face_count == 1 else 0.58
        zoom_delta = (
            5
            if target.size < desired_size - 0.09
            else -5
            if target.size > desired_size + 0.11
            else 0
        )
        new_pan = max(-145, min(145, int(self.state.get("pan", 0)) + pan_delta))
        new_tilt = max(-90, min(100, int(self.state.get("tilt", 0)) + tilt_delta))
        new_zoom = max(100, min(400, current_zoom + zoom_delta))
        if pan_delta == tilt_delta == zoom_delta == 0:
            return False

        def operation() -> tuple[int, int, int]:
            pan = (
                self.camera.set_control("pan", new_pan)
                if pan_delta
                else int(self.state.get("pan", 0))
            )
            tilt = (
                self.camera.set_control("tilt", new_tilt)
                if tilt_delta
                else int(self.state.get("tilt", 0))
            )
            zoom = self.camera.set_control("zoom", new_zoom) if zoom_delta else current_zoom
            return pan, tilt, zoom

        def success(result: tuple[int, int, int]) -> None:
            self.state["pan"], self.state["tilt"], self.state["zoom"] = result

        self._submit("", operation, on_success=success, quiet=True)
        return False

    # ----------------------------------------------------------- phone remote

    def _remote_state(self) -> dict[str, Any]:
        palette = self._palette
        theme = None
        if palette:
            theme = {
                "bg": palette.background,
                "card": palette.lighter_background,
                "fg": palette.foreground,
                "muted": palette.muted,
                "accent": palette.accent,
                "danger": palette.red,
            }
        return {
            "device": self.camera.device.model,
            "status": "Recording" if self.recorder else "Live" if self.preview.running else "Ready",
            "format": self.preview.output_label,
            "zoom": int(self.state.get("zoom", 100)),
            "preview": self.preview.running,
            "record": self.recorder is not None,
            "mirror": bool(self.state.get("mirror", False)),
            "hdr": bool(self.state.get("hdr", False)),
            "privacy": bool(self.state.get("privacy", False)),
            "mode": self.state.get("mode", "normal"),
            "theme": theme,
        }

    def _remote_action(self, action: str, value: Any) -> None:
        allowed = {
            "mode",
            "move",
            "center",
            "zoom",
            "preview",
            "record",
            "screenshot",
            "mirror",
            "hdr",
            "privacy",
        }
        if action not in allowed:
            raise ValueError(f"unsupported action: {action}")
        GLib.idle_add(self._remote_action_main, action, value)

    def _remote_action_main(self, action: str, value: Any) -> bool:
        if self._closed:
            return False
        if action == "mode" and value in self._mode_buttons:
            self._mode_buttons[value].set_active(True)
        elif action == "move" and value in {"up", "down", "left", "right"}:
            deltas = {"up": (0, 5), "down": (0, -5), "left": (-5, 0), "right": (5, 0)}
            self._move_camera(*deltas[value])
        elif action == "center":
            self._center_camera()
        elif action == "zoom":
            try:
                zoom = max(100, min(400, int(value)))
            except (TypeError, ValueError):
                return False
            widget = self._control_widgets.get("zoom")
            if isinstance(widget, Gtk.SpinButton):
                widget.set_value(zoom)
        elif action == "preview":
            self.preview_toggle.set_active(not self.preview_toggle.get_active())
        elif action == "record":
            self.record_button.set_active(not self.record_button.get_active())
        elif action == "screenshot":
            self.take_screenshot()
        elif action in {"mirror", "hdr", "privacy"}:
            widget = self._control_widgets.get(action)
            if isinstance(widget, Gtk.Switch):
                widget.set_active(not widget.get_active())
        return False

    @staticmethod
    def _qr_texture(value: str) -> Gdk.Texture:
        try:
            import qrcode
        except ImportError as exc:
            raise RuntimeError("The qrcode package is not installed") from exc
        stream = io.BytesIO()
        qrcode.make(value).save(stream, format="PNG")
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(stream.getvalue())
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            raise RuntimeError("Could not render the pairing QR code")
        return Gdk.Texture.new_for_pixbuf(pixbuf)

    def _set_remote(self, enabled: bool) -> None:
        if self._updating:
            return
        if enabled:
            try:
                url = self.remote.start()
                self.remote_url_label.set_label(url)
                self.remote_qr.set_paintable(self._qr_texture(url))
                self.remote_qr.set_visible(True)
            except Exception as exc:
                self.remote.stop()
                self._updating = True
                self.remote_switch.set_active(False)
                self._updating = False
                self._toast(f"Phone remote failed: {exc}")
                return
            self._toast("Phone remote is ready on the local network")
        else:
            self.remote.stop()
            self.remote_url_label.set_label("Remote is off")
            self.remote_qr.set_paintable(None)
            self.remote_qr.set_visible(False)

    # ----------------------------------------------------- diagnostics/export

    def export_support_bundle(self) -> None:
        def success(path: Path) -> None:
            self._toast(f"Support bundle saved to {path}")

        self._submit(
            "",
            lambda: create_support_bundle(self.camera, _downloads_dir() / "Link Studio"),
            on_success=success,
            quiet=True,
        )

    # ------------------------------------------------------------- operations

    def _activate_preview(self) -> bool:
        self.preview_toggle.set_active(True)
        return False

    def _preview_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._updating:
            return
        if button.get_active():
            try:
                self.preview.start()
                self._start_preview_poll()
                button.set_icon_name("media-playback-stop-symbolic")
                self.preview_placeholder_label.set_label("Starting preview…")
                self.preview_status.set_label(self.preview.output_label)
                self.compact_status.set_label(self.preview.output_label)
            except Exception as exc:
                self._toast(f"Preview failed: {exc}")
                self._updating = True
                button.set_active(False)
                self._updating = False
        else:
            if self.record_button.get_active():
                self.record_button.set_active(False)
            if hasattr(self, "virtual_camera_switch") and self.virtual_camera_switch.get_active():
                self.virtual_camera_switch.set_active(False)
            self._stop_preview_poll()
            self.preview.stop()
            self.latest_frame = None
            self.picture.set_paintable(None)
            self.preview_placeholder.set_visible(True)
            self.preview_placeholder_label.set_label("Preview is off")
            self.preview_status.set_label("Ready")
            self.compact_status.set_label("Ready")
            button.set_icon_name("media-playback-start-symbolic")

    def _stream_config_changed(self, *_args: object) -> None:
        if not hasattr(self, "preview") or self._changing_stream_controls:
            return
        width, height = self._stream_resolutions[self.resolution_dropdown.get_selected()]
        previous_rate = (
            self._stream_rates[self.fps_dropdown.get_selected()] if self._stream_rates else 30
        )
        supported = list(self.capture_formats.get((width, height), tuple(self._all_stream_rates)))
        if supported and supported != self._stream_rates:
            self._changing_stream_controls = True
            self._stream_rates = supported
            self.fps_dropdown.set_model(
                Gtk.StringList.new([f"{rate} fps" for rate in self._stream_rates])
            )
            chosen = (
                previous_rate
                if previous_rate in supported
                else 30
                if 30 in supported
                else supported[-1]
            )
            self.fps_dropdown.set_selected(supported.index(chosen))
            self._changing_stream_controls = False
        fps = self._stream_rates[self.fps_dropdown.get_selected()]
        was_running = self.preview.running
        if self.virtual_camera:
            self.virtual_camera_switch.set_active(False)
        if was_running:
            self.preview.stop()
        self.preview.config = PreviewConfig(width, height, fps)
        if was_running:
            try:
                self.preview.start()
                self.preview_status.set_label(self.preview.output_label)
                self.compact_status.set_label(self.preview.output_label)
            except Exception as exc:
                self._toast(f"Stream format unavailable: {exc}")
                self._stop_preview_poll()
                self._updating = True
                self.preview_toggle.set_active(False)
                self._updating = False

    def _start_preview_poll(self) -> None:
        if not self._preview_poll_source:
            self._preview_poll_source = GLib.timeout_add(33, self._poll_preview)

    def _stop_preview_poll(self) -> None:
        if self._preview_poll_source:
            GLib.source_remove(self._preview_poll_source)
            self._preview_poll_source = 0

    def _poll_preview(self) -> bool:
        if self._closed:
            self._preview_poll_source = 0
            return False
        error = self.preview.take_error()
        if error:
            self._toast(f"Camera stream: {error}")
        frame = self.preview.take_latest()
        if frame:
            self.latest_frame = frame
            texture = Gdk.MemoryTexture.new(
                frame.width,
                frame.height,
                Gdk.MemoryFormat.R8G8B8,
                GLib.Bytes.new(frame.data),
                frame.stride,
            )
            self.picture.set_paintable(texture)
            self.preview_placeholder.set_visible(False)
        if not self.preview.running and frame is None:
            self._preview_poll_source = 0
            return False
        return True

    def _mode_toggled(self, button: Gtk.ToggleButton, mode: str) -> None:
        if self._updating or not button.get_active():
            return
        previous = str(self.state.get("mode", "normal"))
        self.preview.set_effects(tracking_mode="off")
        tracking_widget = self._control_widgets.get("tracking_engine")
        if isinstance(tracking_widget, Gtk.DropDown):
            self._updating = True
            tracking_widget.set_selected(0)
            self._updating = False

        def success(result: Any) -> None:
            self.state["mode"] = result
            self._sync_mode_buttons(str(result))

        def failure(_exc: BaseException) -> None:
            self._updating = True
            if previous in self._mode_buttons:
                self._mode_buttons[previous].set_active(True)
            self._updating = False

        self._submit(
            f"{VIDEO_MODES[mode][2]} enabled",
            lambda: self.camera.set_video_mode(mode, verify_streaming=self.preview.running),
            on_success=success,
            on_error=failure,
        )

    def _set_standard(self, key: str, value: int | bool) -> None:
        def operation() -> int:
            result = self.camera.set_control(key, value)
            self.state[key] = result
            return result

        self._debounce(key, operation)

    def _set_feature(self, key: str, bit: int, enabled: bool) -> None:
        self._submit(
            f"{key.replace('_', ' ').title()} {'enabled' if enabled else 'disabled'}",
            lambda: self.camera.set_feature(bit, enabled),
            on_success=lambda result: self.state.__setitem__(key, result),
        )

    def _set_privacy(self, enabled: bool) -> None:
        if enabled and self.preview_toggle.get_active():
            self.preview_toggle.set_active(False)
        self._submit(
            f"Privacy mode {'enabled' if enabled else 'disabled'}",
            lambda: self.camera.set_privacy(enabled),
            on_success=lambda result: self.state.__setitem__("privacy", result),
        )

    def _set_autofocus(self, enabled: bool) -> None:
        self.focus_row.set_sensitive(not enabled)
        self._set_standard("focus_auto", enabled)

    def _set_auto_white_balance(self, enabled: bool) -> None:
        self.white_balance_row.set_sensitive(not enabled)
        self._set_standard("white_balance_auto", enabled)

    def _set_auto_exposure(self, enabled: bool) -> None:
        self.iso_row.set_sensitive(not enabled)
        self.shutter_row.set_sensitive(not enabled)
        self._submit(
            f"Automatic exposure {'enabled' if enabled else 'disabled'}",
            lambda: self.camera.set_auto_exposure(enabled),
            on_success=lambda result: self.state.__setitem__("auto_exposure", result),
        )

    def _audio_source_changed(self, selected: int) -> None:
        if not self.audio_sources or selected >= len(self.audio_sources):
            self.selected_audio_source = None
            return
        self.selected_audio_source = self.audio_sources[selected]
        source = self.selected_audio_source
        self._updating = True
        self.audio_mute_switch.set_active(source.muted)
        volume_widget = self._control_widgets.get("audio_volume")
        if isinstance(volume_widget, Gtk.SpinButton):
            volume_widget.set_value(source.volume_percent)
        self._updating = False

    def _set_audio_mute(self, muted: bool) -> None:
        if self._updating or not self.selected_audio_source:
            return
        source_name = self.selected_audio_source.name
        self._submit(
            f"Microphone {'muted' if muted else 'unmuted'}",
            lambda: set_source_mute(source_name, muted),
        )

    def _set_audio_volume(self, percent: int) -> None:
        if self._updating or not self.selected_audio_source:
            return
        source_name = self.selected_audio_source.name
        self._debounce("audio_volume", lambda: set_source_volume(source_name, percent))

    def _set_virtual_camera(self, enabled: bool) -> None:
        if self._updating:
            return
        if enabled:
            frame = self.latest_frame
            if not self.virtual_devices or not self.preview.running or frame is None:
                self._toast("Start the preview before enabling the virtual camera")
                self._updating = True
                self.virtual_camera_switch.set_active(False)
                self._updating = False
                return
            if self.preview.config.fps > 30:
                self._toast("The virtual camera supports up to 30 fps")
                self._updating = True
                self.virtual_camera_switch.set_active(False)
                self._updating = False
                return
            publisher = VirtualCameraPublisher(
                self.virtual_devices[0], frame.width, frame.height, self.preview.config.fps
            )
            try:
                publisher.start()
            except Exception as exc:
                self._toast(f"Virtual camera failed: {exc}")
                self._updating = True
                self.virtual_camera_switch.set_active(False)
                self._updating = False
                return
            self.virtual_camera = publisher
            self.preview.add_consumer(publisher.push)
            self._toast(f"Virtual camera is live on {self.virtual_devices[0]}")
        else:
            publisher, self.virtual_camera = self.virtual_camera, None
            if publisher:
                self.preview.remove_consumer(publisher.push)
                publisher.stop()
                self._toast("Virtual camera stopped")

    def _move_camera(self, pan_delta: int, tilt_delta: int) -> None:
        pan = max(-145, min(145, int(self.state.get("pan", 0)) + pan_delta))
        tilt = max(-90, min(100, int(self.state.get("tilt", 0)) + tilt_delta))

        def operation() -> tuple[int, int]:
            new_pan = self.camera.set_control("pan", pan)
            new_tilt = self.camera.set_control("tilt", tilt)
            return new_pan, new_tilt

        def success(result: tuple[int, int]) -> None:
            self.state["pan"], self.state["tilt"] = result

        self._submit("Gimbal moved", operation, on_success=success, quiet=True)

    def _center_camera(self) -> None:
        def success(_result: Any) -> None:
            self.state.update({"pan": 0, "tilt": 0, "zoom": 100})

        self._submit("Gimbal centered", self.camera.center, on_success=success)

    def _debounce(self, key: str, operation: Callable[[], Any], delay_ms: int = 180) -> None:
        previous = self._pending_sources.pop(key, 0)
        if previous:
            GLib.source_remove(previous)

        def submit() -> bool:
            self._pending_sources.pop(key, None)
            self._submit("", operation, quiet=True)
            return False

        self._pending_sources[key] = GLib.timeout_add(delay_ms, submit)

    def _submit(
        self,
        success_message: str,
        operation: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        quiet: bool = False,
    ) -> None:
        if self._closed:
            return
        self._job_started()
        future = self.executor.submit(operation)

        def completed(done: concurrent.futures.Future[Any]) -> None:
            def main_thread() -> bool:
                self._job_finished()
                try:
                    result = done.result()
                except Exception as exc:
                    LOGGER.exception("Background operation failed", exc_info=exc)
                    if on_error:
                        on_error(exc)
                    self._toast(f"Operation failed: {exc}")
                else:
                    if on_success:
                        on_success(result)
                    if success_message and not quiet:
                        self._toast(success_message)
                return False

            GLib.idle_add(main_thread)

        future.add_done_callback(completed)

    def _job_started(self) -> None:
        with self._job_lock:
            self._jobs += 1
        self.busy_spinner.set_visible(True)
        self.busy_spinner.start()

    def _job_finished(self) -> None:
        with self._job_lock:
            self._jobs = max(0, self._jobs - 1)
            running = self._jobs > 0
        if not running:
            self.busy_spinner.stop()
            self.busy_spinner.set_visible(False)

    # -------------------------------------------------------------- capture

    def take_screenshot(self) -> None:
        frame = self.latest_frame
        if frame is None:
            self._toast("Start the preview before taking a screenshot")
            return
        directory = self.storage_settings.screenshot_directory or (_pictures_dir() / "Link Studio")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"Link-{datetime.now():%Y%m%d-%H%M%S}.png"
        pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
            GLib.Bytes.new(frame.data),
            GdkPixbuf.Colorspace.RGB,
            False,
            8,
            frame.width,
            frame.height,
            frame.stride,
        )
        try:
            pixbuf.savev(str(path), "png", [], [])
        except GLib.Error as exc:
            self._toast(f"Screenshot failed: {exc}")
            return
        if self.ai_recorder.running:
            try:
                self.ai_recorder.add_marker("screenshot", str(path))
            except Exception:
                LOGGER.exception("Could not add the AI-recording screenshot marker")
        self._toast(f"Screenshot saved to {path}")

    def _record_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._updating:
            return
        if button.get_active():
            frame = self.latest_frame
            if frame is None or not self.preview.running:
                self._toast("Start the preview before recording")
                self._updating = True
                button.set_active(False)
                self._updating = False
                return
            directory = self.storage_settings.recording_directory or (_videos_dir() / "Link Studio")
            path = directory / f"Link-{datetime.now():%Y%m%d-%H%M%S}.mp4"
            recorder = Recorder(
                path,
                frame.width,
                frame.height,
                self.preview.config.fps,
                audio_source=(
                    self.selected_audio_source.name if self.selected_audio_source else None
                ),
            )
            try:
                recorder.start()
            except Exception as exc:
                self._toast(f"Recording failed: {exc}")
                self._updating = True
                button.set_active(False)
                self._updating = False
                return
            self.recorder = recorder
            self.preview.add_consumer(recorder.push)
            button.add_css_class("link-recording")
            self.preview_status.set_label("Recording")
        else:
            recorder, self.recorder = self.recorder, None
            button.remove_css_class("link-recording")
            self.preview_status.set_label(
                self.preview.output_label if self.preview.running else "Ready"
            )
            self.compact_status.set_label(
                self.preview.output_label if self.preview.running else "Ready"
            )
            if recorder:
                self.preview.remove_consumer(recorder.push)
                self._submit(
                    "Recording saved",
                    recorder.stop,
                    on_success=lambda path: self._toast(f"Recording saved to {path}"),
                    quiet=True,
                )

    # --------------------------------------------------------------- presets

    @staticmethod
    def _color_preset_values(state: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "hdr",
            "auto_exposure",
            "exposure_compensation",
            "iso",
            "shutter_us",
            "white_balance_auto",
            "white_balance_temperature",
            "brightness",
            "contrast",
            "saturation",
            "hue",
            "sharpness",
            "anti_flicker",
        }
        return {key: value for key, value in state.items() if key in allowed}

    def _refresh_color_preset_list(self) -> None:
        child = self.color_preset_list.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.color_preset_list.remove(child)
            child = following
        if not self.color_presets.presets:
            row = Adw.ActionRow(
                title="No color templates",
                subtitle="Save the current exposure, white balance, and color controls.",
            )
            self.color_preset_list.append(row)
            return
        for index, preset in enumerate(self.color_presets.presets):
            row = Adw.ActionRow(
                title=preset.name,
                subtitle=(
                    f"{preset.values.get('white_balance_temperature', 'Auto')} K · "
                    f"saturation {preset.values.get('saturation', 50)}"
                ),
            )
            apply_button = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
            apply_button.connect(
                "clicked", lambda _button, selected=index: self._apply_color_preset(selected)
            )
            delete_button = Gtk.Button(
                icon_name="user-trash-symbolic",
                tooltip_text="Delete template",
                valign=Gtk.Align.CENTER,
            )
            delete_button.add_css_class("flat")
            delete_button.connect(
                "clicked", lambda _button, selected=index: self._delete_color_preset(selected)
            )
            row.add_suffix(apply_button)
            row.add_suffix(delete_button)
            self.color_preset_list.append(row)

    def _save_color_preset_dialog(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Save color template",
            body="This stores image settings without changing view or tracking modes.",
        )
        entry = Gtk.Entry(placeholder_text="Template name", activates_default=True)
        entry.set_text(f"Color {len(self.color_presets.presets) + 1}")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def response(_dialog: Adw.AlertDialog, response_id: str) -> None:
            if response_id != "save":
                return
            name = entry.get_text()

            def success(values: dict[str, Any]) -> None:
                try:
                    self.color_presets.add(name, self._color_preset_values(values))
                except ValueError as exc:
                    self._toast(str(exc))
                    return
                self._refresh_color_preset_list()
                self._toast(f"Saved color template “{name}”")

            self._submit("", self.camera.read_state, on_success=success, quiet=True)

        dialog.connect("response", response)
        dialog.present(self)

    def _apply_color_preset(self, index: int) -> None:
        preset = self.color_presets.presets[index]

        def operation() -> dict[str, Any]:
            values = preset.values
            if "auto_exposure" in values:
                self.camera.set_auto_exposure(bool(values["auto_exposure"]))
            if "exposure_compensation" in values:
                self.camera.set_exposure_compensation(int(values["exposure_compensation"]))
            if not values.get("auto_exposure", True):
                if "iso" in values:
                    self.camera.set_manual_iso(int(values["iso"]))
                if "shutter_us" in values:
                    self.camera.set_shutter(int(values["shutter_us"]))
            if "white_balance_auto" in values:
                self.camera.set_control("white_balance_auto", bool(values["white_balance_auto"]))
            if not values.get("white_balance_auto", True) and "white_balance_temperature" in values:
                self.camera.set_control(
                    "white_balance_temperature", int(values["white_balance_temperature"])
                )
            for key in (
                "brightness",
                "contrast",
                "saturation",
                "hue",
                "sharpness",
                "anti_flicker",
            ):
                if key in values:
                    self.camera.set_control(key, int(values[key]))
            if "hdr" in values:
                self.camera.set_feature(FEATURE_HDR, bool(values["hdr"]))
            return self.camera.read_state()

        def success(refreshed: dict[str, Any]) -> None:
            self.state.update(preset.values)
            self.state.update(refreshed)
            self._sync_control_widgets({**preset.values, **refreshed})

        self._submit(f"Applied color template “{preset.name}”", operation, on_success=success)

    def _delete_color_preset(self, index: int) -> None:
        name = self.color_presets.presets[index].name
        self.color_presets.remove(index)
        self._refresh_color_preset_list()
        self._toast(f"Deleted color template “{name}”")

    def _refresh_preset_list(self) -> None:
        child = self.preset_list.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.preset_list.remove(child)
            child = following
        if not self.presets.presets:
            row = Adw.ActionRow(
                title="No saved scenes",
                subtitle="Configure the camera, then use “Save current scene”.",
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("view-grid-symbolic"))
            self.preset_list.append(row)
            return
        for index, preset in enumerate(self.presets.presets):
            is_default = index == self.presets.default_index
            summary = self._preset_summary(preset)
            row = Adw.ActionRow(
                title=preset.name,
                subtitle=f"Default · {summary}" if is_default else summary,
            )
            recall = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
            recall.connect("clicked", lambda _button, selected=index: self._apply_preset(selected))
            set_default = Gtk.Button(
                icon_name="starred-symbolic" if is_default else "non-starred-symbolic",
                tooltip_text="Remove as default" if is_default else "Set as default",
                valign=Gtk.Align.CENTER,
            )
            set_default.add_css_class("flat")
            set_default.connect(
                "clicked", lambda _button, selected=index: self._toggle_default_preset(selected)
            )
            update = Gtk.Button(
                icon_name="view-refresh-symbolic",
                tooltip_text="Update with current settings",
                valign=Gtk.Align.CENTER,
            )
            update.add_css_class("flat")
            update.connect("clicked", lambda _button, selected=index: self._update_preset(selected))
            rename = Gtk.Button(
                icon_name="document-edit-symbolic",
                tooltip_text="Rename preset",
                valign=Gtk.Align.CENTER,
            )
            rename.add_css_class("flat")
            rename.connect(
                "clicked", lambda _button, selected=index: self._rename_preset_dialog(selected)
            )
            delete = Gtk.Button(
                icon_name="user-trash-symbolic",
                tooltip_text="Delete preset",
                valign=Gtk.Align.CENTER,
            )
            delete.add_css_class("flat")
            delete.connect(
                "clicked", lambda _button, selected=index: self._delete_preset_dialog(selected)
            )
            row.add_suffix(recall)
            row.add_suffix(set_default)
            row.add_suffix(update)
            row.add_suffix(rename)
            row.add_suffix(delete)
            self.preset_list.append(row)

    def _save_preset_dialog(self) -> None:
        if len(self.presets.presets) >= self.presets.MAX_PRESETS:
            self._toast("The 10-preset limit has been reached")
            return
        dialog = Adw.AlertDialog(
            heading="Save scene preset",
            body="This stores the current camera, view, and smart-mode settings.",
        )
        entry = Gtk.Entry(placeholder_text="Preset name", activates_default=True)
        entry.set_text(f"Scene {len(self.presets.presets) + 1}")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def response(_dialog: Adw.AlertDialog, response_id: str) -> None:
            if response_id != "save":
                return
            name = entry.get_text()

            def success(values: dict[str, Any]) -> None:
                stored = self._preset_values(values)
                stored["software_effects"] = asdict(self.preview.effect_settings)
                try:
                    self.presets.add(name, stored)
                except ValueError as exc:
                    self._toast(str(exc))
                    return
                self._refresh_preset_list()
                self._toast(f"Saved preset “{name}”")

            self._submit("", self.camera.read_state, on_success=success, quiet=True)

        dialog.connect("response", response)
        dialog.present(self)

    def _rename_preset_dialog(self, index: int) -> None:
        preset = self.presets.presets[index]
        dialog = Adw.AlertDialog(heading="Rename scene preset")
        entry = Gtk.Entry(placeholder_text="Preset name", activates_default=True)
        entry.set_text(preset.name)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def response(_dialog: Adw.AlertDialog, response_id: str) -> None:
            if response_id != "rename":
                return
            try:
                self.presets.rename(index, entry.get_text())
            except ValueError as exc:
                self._toast(str(exc))
                return
            self._refresh_preset_list()
            self._toast("Preset renamed")

        dialog.connect("response", response)
        dialog.present(self)

    def _update_preset(self, index: int) -> None:
        name = self.presets.presets[index].name

        def success(values: dict[str, Any]) -> None:
            stored = self._preset_values(values)
            stored["software_effects"] = asdict(self.preview.effect_settings)
            self.presets.update(index, stored)
            self._refresh_preset_list()
            self._toast(f"Updated preset “{name}”")

        self._submit("", self.camera.read_state, on_success=success, quiet=True)

    def _toggle_default_preset(self, index: int) -> None:
        preset = self.presets.presets[index]
        if self.presets.default_index == index:
            self.presets.set_default(None)
            message = "Default preset removed"
        else:
            self.presets.set_default(index)
            message = f"“{preset.name}” will be applied when the camera connects"
        self._refresh_preset_list()
        self._toast(message)

    def _apply_default_preset(self) -> bool:
        index = self.presets.default_index
        if index is not None and 0 <= index < len(self.presets.presets):
            self._apply_preset(index, startup=True)
        return False

    @staticmethod
    def _preset_values(state: dict[str, Any]) -> dict[str, Any]:
        allowed = set(STANDARD_CONTROLS) | {
            "mode",
            "hdr",
            "mirror",
            "gesture_zoom",
            "privacy",
            "auto_exposure",
            "exposure_compensation",
            "tracking_speed",
            "framing",
            "noise_cancellation",
            "audio_mode",
            "iso",
            "shutter_us",
        }
        return {key: value for key, value in state.items() if key in allowed}

    @staticmethod
    def _preset_summary(preset: Preset) -> str:
        mode = str(preset.values.get("mode", "normal")).replace("_", " ").title()
        zoom = preset.values.get("zoom", 100)
        software = preset.values.get("software_effects", {})
        effect = software.get("mode", "none") if isinstance(software, dict) else "none"
        effect_label = "" if effect == "none" else f" · {str(effect).replace('_', ' ').title()}"
        return f"{mode} · {zoom}% zoom{effect_label}"

    def _apply_preset(self, index: int, startup: bool = False) -> None:
        preset = self.presets.presets[index]

        def operation() -> tuple[str, dict[str, Any]]:
            values = preset.values
            for key in (
                "brightness",
                "contrast",
                "saturation",
                "hue",
                "sharpness",
                "anti_flicker",
                "zoom",
                "pan",
                "tilt",
            ):
                if key in values:
                    self.camera.set_control(key, int(values[key]))
            if "focus_auto" in values:
                self.camera.set_control("focus_auto", bool(values["focus_auto"]))
            if not values.get("focus_auto", True) and "focus" in values:
                self.camera.set_control("focus", int(values["focus"]))
            if "white_balance_auto" in values:
                self.camera.set_control("white_balance_auto", bool(values["white_balance_auto"]))
            if not values.get("white_balance_auto", True) and "white_balance_temperature" in values:
                self.camera.set_control(
                    "white_balance_temperature", int(values["white_balance_temperature"])
                )
            if "auto_exposure" in values:
                self.camera.set_auto_exposure(bool(values["auto_exposure"]))
            if "exposure_compensation" in values:
                self.camera.set_exposure_compensation(int(values["exposure_compensation"]))
            if not values.get("auto_exposure", True):
                if "iso" in values:
                    self.camera.set_manual_iso(int(values["iso"]))
                if "shutter_us" in values:
                    self.camera.set_shutter(int(values["shutter_us"]))
            for key, bit in (
                ("hdr", FEATURE_HDR),
                ("mirror", FEATURE_MIRROR),
                ("gesture_zoom", FEATURE_GESTURE_ZOOM),
            ):
                if key in values:
                    self.camera.set_feature(bit, bool(values[key]))
            if "framing" in values and values["framing"] in FRAMING_MODES:
                self.camera.set_framing(str(values["framing"]))
            if "tracking_speed" in values:
                self.camera.set_tracking_speed(int(values["tracking_speed"]))
            if "audio_mode" in values:
                self.camera.set_audio_mode(str(values["audio_mode"]))
            elif "noise_cancellation" in values:
                self.camera.set_noise_cancellation(bool(values["noise_cancellation"]))
            software = values.get("software_effects")
            if isinstance(software, dict):
                restored = dict(software)
                if isinstance(restored.get("tracking_area"), list):
                    restored["tracking_area"] = tuple(restored["tracking_area"])
                if isinstance(restored.get("pause_areas"), list):
                    restored["pause_areas"] = tuple(
                        tuple(region) for region in restored["pause_areas"]
                    )
                self.preview.set_effects(**restored)
            mode = str(values.get("mode", "normal"))
            if mode in VIDEO_MODES:
                self.camera.set_video_mode(mode, verify_streaming=self.preview.running)
            return mode, self.camera.read_state()

        def success(result: tuple[str, dict[str, Any]]) -> None:
            mode, refreshed = result
            self.state.update(preset.values)
            self.state.update(refreshed)
            sync_values = {**preset.values, **refreshed}
            software = preset.values.get("software_effects")
            if isinstance(software, dict):
                sync_values.update(
                    {
                        "orientation": software.get("orientation", "identity"),
                        "effect_mode": software.get("mode", "none"),
                        "effect_intensity": software.get("intensity", 55),
                        "key_tolerance": software.get("key_tolerance", 70),
                    }
                )
            self._sync_control_widgets(sync_values)
            self._sync_mode_buttons(mode)

        message = (
            f"Applied default preset “{preset.name}”"
            if startup
            else f"Applied preset “{preset.name}”"
        )
        self._submit(message, operation, on_success=success)

    def _delete_preset_dialog(self, index: int) -> None:
        preset = self.presets.presets[index]
        dialog = Adw.AlertDialog(
            heading=f"Delete “{preset.name}”?",
            body="This scene preset cannot be restored.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _dialog, response_id: (
                self._delete_preset(index) if response_id == "delete" else None
            ),
        )
        dialog.present(self)

    def _delete_preset(self, index: int) -> None:
        name = self.presets.presets[index].name
        self.presets.remove(index)
        self._refresh_preset_list()
        self._toast(f"Deleted preset “{name}”")

    # --------------------------------------------------------------- theming

    def _theme_applied(self, palette: Palette | None) -> None:
        self._palette = palette
        if not hasattr(self, "window_title"):
            return
        self.window_title.set_subtitle(f"{self.camera.device.model} · {self.camera.device.path}")
        if palette and self.preview.effect_settings.background_color == "#242424":
            rgba = Gdk.RGBA()
            rgba.parse(palette.background)
            if hasattr(self, "background_color_button"):
                self.background_color_button.set_rgba(rgba)

    def _toast(self, message: str) -> None:
        if not self._closed:
            self.toast_overlay.add_toast(Adw.Toast(title=message, timeout=4))

    # --------------------------------------------------------------- teardown

    def _on_close_request(self, *_args: object) -> bool:
        self._closed = True
        self.remote.stop()
        if self._ai_recording_timer:
            GLib.source_remove(self._ai_recording_timer)
            self._ai_recording_timer = 0
        if self.ai_recorder.running:
            try:
                self.ai_recorder.stop()
            except Exception:
                LOGGER.exception("Could not finalize the local AI recording")
        for teleprompter in tuple(self._teleprompter_windows):
            teleprompter.close()
        self.preview.processor.set_tracking_callback(None)
        self._stop_preview_poll()
        for source_id in self._pending_sources.values():
            GLib.source_remove(source_id)
        self._pending_sources.clear()
        recorder, self.recorder = self.recorder, None
        if recorder:
            self.preview.remove_consumer(recorder.push)
            with suppress(Exception):
                recorder.stop()
        publisher, self.virtual_camera = self.virtual_camera, None
        if publisher:
            self.preview.remove_consumer(publisher.push)
            publisher.stop()
        self.preview.close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.camera.close()
        return False
