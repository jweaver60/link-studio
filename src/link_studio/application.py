from __future__ import annotations

import logging

import gi

gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gio, Gtk

from . import __version__
from .camera import Camera, CameraError, discover_cameras
from .constants import APP_ID, APP_NAME
from .diagnostics import configure_logging
from .shortcuts import GlobalShortcutPortal

LOGGER = logging.getLogger("link_studio.application")


class LinkStudioApplication(Adw.Application):
    def __init__(self, device_path: str | None = None, start_preview: bool = True):
        configure_logging()
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.device_path = device_path
        self.start_preview = start_preview
        self.window = None
        self.global_shortcuts = GlobalShortcutPortal(
            self._global_shortcut_activated, self._global_shortcut_status
        )

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_args: self.quit())
        self.add_action(quit_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.add_action(about_action)

        support_action = Gio.SimpleAction.new("support-bundle", None)
        support_action.connect("activate", self._export_support_bundle)
        self.add_action(support_action)

        self.set_accels_for_action("app.quit", ["<primary>q"])
        self.set_accels_for_action("win.screenshot", ["<primary><shift>s"])
        self.set_accels_for_action("win.preview", ["<primary>p"])
        self.set_accels_for_action("win.record", ["<primary>r"])
        self.set_accels_for_action("win.compact", ["<primary>m"])

    def do_activate(self) -> None:
        if self.window:
            self.window.present()
            return

        devices = discover_cameras()
        selected = next((device for device in devices if device.path == self.device_path), None)
        if selected is None and devices:
            selected = devices[0]
        if selected is None:
            LOGGER.warning("No supported Insta360 camera was found")
            self.window = self._no_camera_window()
            self.window.present()
            return

        try:
            camera = Camera(selected)
        except (OSError, CameraError) as exc:
            LOGGER.exception("Could not open %s", selected.path)
            self.window = self._error_window(str(exc))
            self.window.present()
            return

        from .window import LinkStudioWindow

        self.window = LinkStudioWindow(self, camera, start_preview=self.start_preview)
        LOGGER.info("Opened %s at %s", selected.model, selected.path)
        self.window.present()
        if self.global_shortcuts.settings.enabled:
            self.global_shortcuts.start()

    def _global_shortcut_activated(self, identifier: str) -> bool:
        if self.window and hasattr(self.window, "activate_global_shortcut"):
            self.window.activate_global_shortcut(identifier)
        return False

    def _global_shortcut_status(self, message: str, enabled: bool) -> bool:
        if self.window and hasattr(self.window, "global_shortcut_status_changed"):
            self.window.global_shortcut_status_changed(message, enabled)
        return False

    def set_global_shortcuts(self, enabled: bool) -> None:
        if enabled:
            self.global_shortcuts.start()
        else:
            self.global_shortcuts.stop()

    def configure_global_shortcuts(self) -> None:
        self.global_shortcuts.configure()

    def do_shutdown(self) -> None:
        self.global_shortcuts.close()
        Adw.Application.do_shutdown(self)

    def _no_camera_window(self) -> Adw.ApplicationWindow:
        window = Adw.ApplicationWindow(application=self, title=APP_NAME, default_width=640)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        status = Adw.StatusPage(
            icon_name="camera-web-symbolic",
            title="No Insta360 Link found",
            description="Connect the camera over USB, then try again.",
        )
        retry = Gtk.Button(label="Try Again")
        retry.add_css_class("suggested-action")
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_args: self._retry_detection(window))
        status.set_child(retry)
        toolbar.set_content(status)
        window.set_content(toolbar)
        return window

    def _error_window(self, message: str) -> Adw.ApplicationWindow:
        window = Adw.ApplicationWindow(application=self, title=APP_NAME, default_width=640)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        status = Adw.StatusPage(
            icon_name="dialog-error-symbolic",
            title="Could not open the camera",
            description=message,
        )
        toolbar.set_content(status)
        window.set_content(toolbar)
        return window

    def _retry_detection(self, old_window: Adw.ApplicationWindow) -> None:
        old_window.close()
        self.window = None
        self.activate()

    def _show_about(self, *_args: object) -> None:
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="camera-web-symbolic",
            developer_name="Link Studio contributors",
            version=__version__,
            license_type=Gtk.License.MIT_X11,
            comments="An unofficial, native Linux controller for Insta360 Link webcams.",
        )
        if self.window:
            dialog.present(self.window)

    def _export_support_bundle(self, *_args: object) -> None:
        if self.window and hasattr(self.window, "export_support_bundle"):
            self.window.export_support_bundle()
