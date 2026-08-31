from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from itertools import count
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GLib", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

_STYLE_SCOPE_IDS = count(1)


def default_script_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "link-studio/teleprompter.json"


@dataclass(slots=True)
class TeleprompterScript:
    name: str
    text: str


class ScriptStore:
    MAX_SCRIPTS = 100
    MAX_CHARACTERS = 100_000

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_script_path()
        self.scripts: list[TeleprompterScript] = []
        self.load()

    def load(self) -> list[TeleprompterScript]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.scripts = []
            return self.scripts
        loaded = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            name, text = item.get("name"), item.get("text")
            if isinstance(name, str) and name.strip() and isinstance(text, str):
                loaded.append(TeleprompterScript(name.strip()[:80], text[: self.MAX_CHARACTERS]))
        self.scripts = loaded[: self.MAX_SCRIPTS]
        return self.scripts

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix="teleprompter-", suffix=".json", dir=self.path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump([asdict(script) for script in self.scripts], handle, indent=2)
                handle.write("\n")
            temporary_path.replace(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def add(self, name: str, text: str) -> TeleprompterScript:
        if len(self.scripts) >= self.MAX_SCRIPTS:
            raise ValueError("The teleprompter script limit has been reached")
        script = TeleprompterScript(self._clean_name(name), text[: self.MAX_CHARACTERS])
        self.scripts.append(script)
        self.save()
        return script

    def update(self, index: int, name: str, text: str) -> None:
        self.scripts[index] = TeleprompterScript(
            self._clean_name(name), text[: self.MAX_CHARACTERS]
        )
        self.save()

    def remove(self, index: int) -> None:
        del self.scripts[index]
        self.save()

    @staticmethod
    def _clean_name(name: str) -> str:
        clean = name.strip()[:80]
        if not clean:
            raise ValueError("Script name cannot be empty")
        return clean


class TeleprompterWindow(Gtk.ApplicationWindow):
    """Adjustable, auto-scrolling teleprompter player."""

    def __init__(self, application: Gtk.Application, script: TeleprompterScript) -> None:
        super().__init__(
            application=application,
            title=f"Teleprompter — {script.name}",
            default_width=900,
            default_height=560,
        )
        self._timer = 0
        self._countdown = 0
        self._playing = False
        self._css = Gtk.CssProvider()
        self._style_scope = f"link-teleprompter-{next(_STYLE_SCOPE_IDS)}"
        self._guide_scope = f"{self._style_scope}-guide"
        self._countdown_scope = f"{self._style_scope}-countdown"
        self._text_rgba = Gdk.RGBA()
        self._text_rgba.parse("#ffffff")
        self._background_rgba = Gdk.RGBA()
        self._background_rgba.parse("#08080c")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_margin_top(8)
        controls.set_margin_bottom(8)
        controls.set_margin_start(10)
        controls.set_margin_end(10)

        self.play = Gtk.ToggleButton(label="Play")
        self.play.connect("toggled", self._play_toggled)
        controls.append(self.play)
        top = Gtk.Button(label="Back to Top")
        top.connect("clicked", lambda *_args: self._back_to_top())
        controls.append(top)
        controls.append(Gtk.Label(label="Speed"))
        self.speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 180, 5)
        self.speed.set_value(55)
        self.speed.set_size_request(150, -1)
        controls.append(self.speed)
        controls.append(Gtk.Label(label="Text"))
        self.font_size = Gtk.SpinButton.new_with_range(24, 120, 2)
        self.font_size.set_value(54)
        self.font_size.connect("value-changed", lambda *_args: self._apply_style())
        controls.append(self.font_size)
        self.loop = Gtk.CheckButton(label="Loop")
        controls.append(self.loop)
        self.guide = Gtk.CheckButton(label="Reading guide", active=True)
        self.guide.connect(
            "toggled", lambda button: self.guide_line.set_visible(button.get_active())
        )
        controls.append(self.guide)
        settings_button = Gtk.MenuButton(label="Display")
        settings_button.set_popover(self._build_display_popover())
        controls.append(settings_button)
        root.append(controls)

        overlay = Gtk.Overlay(vexpand=True, hexpand=True)
        self.scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.text = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            left_margin=70,
            right_margin=70,
            top_margin=180,
            bottom_margin=300,
        )
        self.text.add_css_class(self._style_scope)
        self.text.get_buffer().set_text(script.text)
        self.scroll.set_child(self.text)
        overlay.set_child(self.scroll)
        self.guide_line = Gtk.Separator(
            orientation=Gtk.Orientation.HORIZONTAL,
            valign=Gtk.Align.CENTER,
            can_target=False,
        )
        self.guide_line.add_css_class(self._guide_scope)
        self.guide_line.set_margin_start(35)
        self.guide_line.set_margin_end(35)
        overlay.add_overlay(self.guide_line)
        self.countdown_label = Gtk.Label(
            label="", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, can_target=False
        )
        self.countdown_label.add_css_class(self._countdown_scope)
        overlay.add_overlay(self.countdown_label)
        root.append(overlay)
        self.set_child(root)
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(), self._css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._apply_style()
        self.connect("close-request", self._closed)

    def _build_display_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        def row(label: str, child: Gtk.Widget) -> None:
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            item.append(Gtk.Label(label=label, hexpand=True, xalign=0))
            item.append(child)
            box.append(item)

        text_color = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(title="Choose text color"), rgba=self._text_rgba
        )
        text_color.connect("notify::rgba", self._text_color_changed)
        row("Text color", text_color)
        background_color = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(title="Choose background color"),
            rgba=self._background_rgba,
        )
        background_color.connect("notify::rgba", self._background_color_changed)
        row("Background", background_color)
        self.background_opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 20, 100, 5)
        self.background_opacity.set_value(94)
        self.background_opacity.set_size_request(150, -1)
        self.background_opacity.connect("value-changed", lambda *_args: self._apply_style())
        row("Opacity", self.background_opacity)
        self.countdown_enabled = Gtk.CheckButton(label="Countdown", active=True)
        box.append(self.countdown_enabled)
        self.countdown_seconds = Gtk.SpinButton.new_with_range(1, 10, 1)
        self.countdown_seconds.set_value(3)
        row("Countdown time", self.countdown_seconds)
        popover.set_child(box)
        return popover

    def _text_color_changed(self, button: Gtk.ColorDialogButton, _param: object) -> None:
        self._text_rgba = button.get_rgba()
        self._apply_style()

    def _background_color_changed(self, button: Gtk.ColorDialogButton, _param: object) -> None:
        self._background_rgba = button.get_rgba()
        self._apply_style()

    @staticmethod
    def _css_rgb(color: Gdk.RGBA) -> str:
        return f"{round(color.red * 255)},{round(color.green * 255)},{round(color.blue * 255)}"

    def _apply_style(self) -> None:
        text = self._css_rgb(self._text_rgba)
        background = self._css_rgb(self._background_rgba)
        opacity = (
            self.background_opacity.get_value() / 100
            if hasattr(self, "background_opacity")
            else 0.94
        )
        self._css.load_from_string(
            f".{self._style_scope} {{"
            f"font-size: {self.font_size.get_value_as_int()}px;"
            f"line-height: 1.35; color: rgb({text}); background: rgba({background},{opacity:.2f});"
            f"}} .{self._guide_scope} {{background:#89b4fa;min-height:2px;opacity:.7;}}"
            f".{self._countdown_scope} "
            f"{{font-size:96px;font-weight:800;color:rgb({text});}}"
        )

    def _play_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            self._countdown = (
                self.countdown_seconds.get_value_as_int()
                if self.countdown_enabled.get_active()
                else 0
            )
            if self._countdown:
                self.countdown_label.set_label(str(self._countdown))
                self.countdown_label.set_visible(True)
                self._playing = False
                if not self._timer:
                    self._timer = GLib.timeout_add(1000, self._tick)
            else:
                self._playing = True
                button.set_label("Pause")
                if not self._timer:
                    self._timer = GLib.timeout_add(33, self._scroll_tick)
        else:
            self._playing = False
            self.countdown_label.set_visible(False)
            button.set_label("Play")
            if self._timer:
                GLib.source_remove(self._timer)
                self._timer = 0

    def _tick(self) -> bool:
        if not self.play.get_active():
            self._timer = 0
            return False
        if self._countdown:
            self._countdown -= 1
            if self._countdown:
                self.countdown_label.set_label(str(self._countdown))
                return True
            self.countdown_label.set_visible(False)
            self._playing = True
            self.play.set_label("Pause")
            self._timer = GLib.timeout_add(33, self._scroll_tick)
            return False
        return True

    def _scroll_tick(self) -> bool:
        if not self.play.get_active() or not self._playing:
            self._timer = 0
            return False
        adjustment = self.scroll.get_vadjustment()
        maximum = max(0.0, adjustment.get_upper() - adjustment.get_page_size())
        position = adjustment.get_value() + self.speed.get_value() / 30.0
        if position >= maximum:
            if self.loop.get_active():
                position = 0
            else:
                self._timer = 0
                self.play.set_active(False)
                return False
        adjustment.set_value(position)
        return True

    def _back_to_top(self) -> None:
        self.scroll.get_vadjustment().set_value(0)

    def _closed(self, *_args: object) -> bool:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        Gtk.StyleContext.remove_provider_for_display(self.get_display(), self._css)
        return False
