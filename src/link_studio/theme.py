from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def omarchy_current_dir() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "omarchy/current"


@dataclass(frozen=True, slots=True)
class Palette:
    name: str
    mode: str
    accent: str
    selection: str
    muted: str
    background: str
    dark_background: str
    darker_background: str
    lighter_background: str
    foreground: str
    dark_foreground: str
    red: str
    yellow: str
    green: str
    blue: str

    @property
    def is_dark(self) -> bool:
        return self.mode.casefold() != "light"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _safe_color(data: dict[str, object], key: str, fallback: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and HEX_COLOR.fullmatch(value):
        return value.lower()
    return fallback


def load_palette(colors_path: Path, name: str = "Omarchy") -> Palette | None:
    try:
        with colors_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    return Palette(
        name=name,
        mode=str(data.get("mode", "dark")),
        accent=_safe_color(data, "accent", "#3584e4"),
        selection=_safe_color(data, "selection", "#1c71d8"),
        muted=_safe_color(data, "muted", "#77767b"),
        background=_safe_color(data, "background", "#242424"),
        dark_background=_safe_color(data, "dark_background", "#1e1e1e"),
        darker_background=_safe_color(data, "darker_background", "#151515"),
        lighter_background=_safe_color(data, "lighter_background", "#303030"),
        foreground=_safe_color(data, "foreground", "#ffffff"),
        dark_foreground=_safe_color(data, "dark_foreground", "#b0b0b0"),
        red=_safe_color(data, "red", "#e01b24"),
        yellow=_safe_color(data, "yellow", "#f6d32d"),
        green=_safe_color(data, "green", "#33d17a"),
        blue=_safe_color(data, "blue", "#3584e4"),
    )


def load_current_omarchy_palette() -> Palette | None:
    current = omarchy_current_dir()
    try:
        theme_name = (current / "theme.name").read_text().strip()
    except OSError:
        theme_name = "Omarchy"
    return load_palette(current / "theme/colors.toml", theme_name or "Omarchy")


def _contrast_color(hex_color: str) -> str:
    red, green, blue = (int(hex_color[index : index + 2], 16) for index in (1, 3, 5))
    luminance = (red * 299 + green * 587 + blue * 114) / 1000
    return "#151515" if luminance > 150 else "#ffffff"


def palette_css(palette: Palette) -> str:
    accent_foreground = _contrast_color(palette.accent)
    selection_foreground = _contrast_color(palette.selection)
    return f"""
@define-color accent_color {palette.accent};
@define-color accent_bg_color {palette.accent};
@define-color accent_fg_color {accent_foreground};
@define-color window_bg_color {palette.background};
@define-color window_fg_color {palette.foreground};
@define-color view_bg_color {palette.dark_background};
@define-color view_fg_color {palette.foreground};
@define-color headerbar_bg_color {palette.dark_background};
@define-color headerbar_fg_color {palette.foreground};
@define-color card_bg_color {palette.lighter_background};
@define-color card_fg_color {palette.foreground};
@define-color popover_bg_color {palette.lighter_background};
@define-color popover_fg_color {palette.foreground};
@define-color sidebar_bg_color {palette.dark_background};
@define-color sidebar_fg_color {palette.foreground};
@define-color dialog_bg_color {palette.background};
@define-color dialog_fg_color {palette.foreground};
@define-color destructive_bg_color {palette.red};
@define-color destructive_fg_color {_contrast_color(palette.red)};
@define-color success_bg_color {palette.green};
@define-color success_fg_color {_contrast_color(palette.green)};
@define-color warning_bg_color {palette.yellow};
@define-color warning_fg_color {_contrast_color(palette.yellow)};

.link-preview-frame {{
  background: {palette.darker_background};
  border-radius: 14px;
  box-shadow: 0 1px 0 alpha({palette.foreground}, 0.08);
}}

.link-preview-placeholder {{ color: {palette.dark_foreground}; }}
.link-mode-bar {{
  background: alpha({palette.darker_background}, 0.92);
  border: 1px solid alpha({palette.foreground}, 0.10);
  border-radius: 999px;
  padding: 6px;
}}
.link-status-ok {{ color: {palette.green}; }}
.link-status-warn {{ color: {palette.yellow}; }}
.link-status-error {{ color: {palette.red}; }}
.link-muted {{ color: {palette.dark_foreground}; }}
.link-recording {{ color: {palette.red}; }}
.link-accent {{ color: {palette.accent}; }}
selection {{ background: {palette.selection}; color: {selection_foreground}; }}
"""


class OmarchyThemeBridge:
    """Apply and live-reload the active Omarchy palette in a GTK application."""

    def __init__(self, on_applied: Callable[[Palette | None], None] | None = None):
        self.on_applied = on_applied
        self.palette: Palette | None = None
        self._provider = None
        self._monitors: list[object] = []
        self._reload_source = 0

    def start(self) -> None:
        import gi

        gi.require_version("Adw", "1")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gio, Gtk

        self._provider = Gtk.CssProvider()
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        current = omarchy_current_dir()
        for directory in (current, current / "theme"):
            try:
                monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None
                )
                monitor.connect("changed", self._on_changed)
                self._monitors.append(monitor)
            except Exception:
                continue
        self.apply()

    def _on_changed(self, *_args: object) -> None:
        from gi.repository import GLib

        if self._reload_source:
            GLib.source_remove(self._reload_source)
        self._reload_source = GLib.timeout_add(150, self._reload)

    def _reload(self) -> bool:
        self._reload_source = 0
        self.apply()
        return False

    def apply(self) -> None:
        from gi.repository import Adw

        palette = load_current_omarchy_palette()
        self.palette = palette
        if palette and self._provider:
            self._provider.load_from_string(palette_css(palette))
            scheme = Adw.ColorScheme.FORCE_DARK if palette.is_dark else Adw.ColorScheme.FORCE_LIGHT
            Adw.StyleManager.get_default().set_color_scheme(scheme)
        elif self._provider:
            self._provider.load_from_string("")
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
        if self.on_applied:
            self.on_applied(palette)
