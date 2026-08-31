from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from .constants import APP_ID

LOGGER = logging.getLogger("link_studio.shortcuts")

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
HOST_REGISTRY_INTERFACE = "org.freedesktop.host.portal.Registry"


@dataclass(frozen=True, slots=True)
class Shortcut:
    identifier: str
    description: str


SHORTCUTS = (
    Shortcut("preview", "Start or stop camera preview"),
    Shortcut("record", "Start or stop video recording"),
    Shortcut("screenshot", "Take a screenshot"),
    Shortcut("compact", "Toggle compact toolbar"),
    Shortcut("tracking", "Toggle AI Tracking"),
    Shortcut("whiteboard", "Toggle Whiteboard mode"),
    Shortcut("gimbal_up", "Move gimbal up"),
    Shortcut("gimbal_down", "Move gimbal down"),
    Shortcut("gimbal_left", "Move gimbal left"),
    Shortcut("gimbal_right", "Move gimbal right"),
    Shortcut("center", "Center gimbal"),
    Shortcut("zoom_in", "Zoom in"),
    Shortcut("zoom_out", "Zoom out"),
    Shortcut("privacy", "Toggle privacy mode"),
)


def default_shortcut_settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "link-studio/shortcuts.json"


class ShortcutSettings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_shortcut_settings_path()
        self.enabled = False
        self.load()

    def load(self) -> bool:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.enabled = False
        else:
            self.enabled = bool(value.get("enabled", False)) if isinstance(value, dict) else False
        return self.enabled

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix="shortcuts-", suffix=".json", dir=self.path.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "enabled": self.enabled}, handle, indent=2)
                handle.write("\n")
            temporary_path.replace(self.path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _unpack(value: Any) -> Any:
    return value.unpack() if isinstance(value, GLib.Variant) else value


def register_portal_identity() -> Gio.DBusConnection:
    """Associate the process with Link Studio before GTK contacts the portal."""

    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    try:
        connection.call_sync(
            PORTAL_NAME,
            PORTAL_PATH,
            HOST_REGISTRY_INTERFACE,
            "Register",
            GLib.Variant("(sa{sv})", (APP_ID, {})),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
    except GLib.Error as exc:
        remote_error = Gio.DBusError.get_remote_error(exc) or ""
        if remote_error not in {
            "org.freedesktop.DBus.Error.UnknownInterface",
            "org.freedesktop.DBus.Error.UnknownMethod",
        }:
            raise
        LOGGER.info("Desktop portal host-app registration is unavailable")
    return connection


def try_register_portal_identity() -> tuple[Gio.DBusConnection | None, str | None]:
    """Return an early portal connection without making app startup fatal."""

    try:
        return register_portal_identity(), None
    except GLib.Error as exc:
        return None, exc.message


class GlobalShortcutPortal:
    """XDG portal session for compositor-managed, remappable global shortcuts."""

    def __init__(
        self,
        activated: Callable[[str], None],
        status_changed: Callable[[str, bool], None] | None = None,
        settings: ShortcutSettings | None = None,
    ) -> None:
        self.activated = activated
        self.status_changed = status_changed
        self.settings = settings or ShortcutSettings()
        self.proxy: Gio.DBusProxy | None = None
        self.connection: Gio.DBusConnection | None = None
        self.session_handle: str | None = None
        self._requests: dict[str, str] = {}
        self._response_subscription = 0
        self._activated_subscription = 0

    @property
    def running(self) -> bool:
        return self.session_handle is not None

    def _status(self, message: str, enabled: bool) -> None:
        if self.status_changed:
            GLib.idle_add(self.status_changed, message, enabled)

    def available(self) -> bool:
        try:
            self._ensure_proxy()
        except GLib.Error:
            return False
        return True

    def prepare(self) -> None:
        """Register this native process before making any desktop portal request."""

        if self.connection:
            return
        self.connection = register_portal_identity()

    def _ensure_proxy(self) -> Gio.DBusProxy:
        if self.proxy:
            return self.proxy
        self.prepare()
        if not self.connection:
            raise RuntimeError("Desktop portal session bus is unavailable")
        proxy = Gio.DBusProxy.new_sync(
            self.connection,
            Gio.DBusProxyFlags.NONE,
            None,
            PORTAL_NAME,
            PORTAL_PATH,
            PORTAL_INTERFACE,
            None,
        )
        self.proxy = proxy
        self._response_subscription = self.connection.signal_subscribe(
            PORTAL_NAME,
            "org.freedesktop.portal.Request",
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            self._request_response,
        )
        self._activated_subscription = self.connection.signal_subscribe(
            PORTAL_NAME,
            PORTAL_INTERFACE,
            "Activated",
            PORTAL_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._shortcut_activated,
        )
        return proxy

    def start(self) -> None:
        if self.running or self._requests:
            return
        try:
            proxy = self._ensure_proxy()
            request_token = f"linkstudio_{secrets.token_hex(8)}"
            session_token = f"linkstudio_{secrets.token_hex(8)}"
            options = {
                "handle_token": GLib.Variant("s", request_token),
                "session_handle_token": GLib.Variant("s", session_token),
            }
            result = proxy.call_sync(
                "CreateSession",
                GLib.Variant("(a{sv})", (options,)),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            request_path = result.unpack()[0]
            self._requests[request_path] = "create"
            self._status("Waiting for the desktop portal…", True)
        except GLib.Error as exc:
            self.settings.set_enabled(False)
            self._status(f"Global shortcuts unavailable: {exc.message}", False)

    def configure(self) -> None:
        if not self.running:
            self.start()
            return
        proxy = self._ensure_proxy()
        version_value = proxy.get_cached_property("version")
        version = int(version_value.unpack()) if version_value else 1
        if version >= 2:
            try:
                proxy.call_sync(
                    "ConfigureShortcuts",
                    GLib.Variant("(osa{sv})", (self.session_handle, "", {})),
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
            except GLib.Error as exc:
                self._status(f"Shortcut settings could not be opened: {exc.message}", True)
        else:
            self._status(
                "Actions are registered; assign keys with Hyprland's global dispatcher",
                True,
            )

    def _bind(self) -> None:
        if not self.proxy or not self.session_handle:
            return
        shortcuts = [
            (
                shortcut.identifier,
                {"description": GLib.Variant("s", shortcut.description)},
            )
            for shortcut in SHORTCUTS
        ]
        request_token = f"linkstudio_{secrets.token_hex(8)}"
        result = self.proxy.call_sync(
            "BindShortcuts",
            GLib.Variant(
                "(oa(sa{sv})sa{sv})",
                (
                    self.session_handle,
                    shortcuts,
                    "",
                    {"handle_token": GLib.Variant("s", request_token)},
                ),
            ),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self._requests[result.unpack()[0]] = "bind"

    def _request_response(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        object_path: str,
        _interface: str,
        _signal: str,
        parameters: GLib.Variant,
        *_user_data: object,
    ) -> None:
        kind = self._requests.pop(object_path, None)
        if not kind:
            return
        response, results = parameters.unpack()
        if response != 0:
            self.settings.set_enabled(False)
            self._status("Global shortcut setup was cancelled", False)
            if kind == "create":
                self.session_handle = None
            return
        if kind == "create":
            session = _unpack(results.get("session_handle"))
            if not isinstance(session, str):
                self.settings.set_enabled(False)
                self._status("The desktop portal returned no shortcut session", False)
                return
            self.session_handle = session
            try:
                self._bind()
            except GLib.Error as exc:
                self.settings.set_enabled(False)
                self._status(f"Could not bind global shortcuts: {exc.message}", False)
            return
        self.settings.set_enabled(True)
        bound = _unpack(results.get("shortcuts", []))
        count = len(bound) if isinstance(bound, (list, tuple)) else len(SHORTCUTS)
        self._status(f"Global shortcut actions ready · {count} registered", True)

    def _shortcut_activated(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        _interface: str,
        _signal: str,
        parameters: GLib.Variant,
        *_user_data: object,
    ) -> None:
        session, identifier, _timestamp, _options = parameters.unpack()
        if session == self.session_handle and identifier in {item.identifier for item in SHORTCUTS}:
            GLib.idle_add(self.activated, identifier)

    def stop(self, remember: bool = False) -> None:
        if self.connection and self.session_handle:
            with suppress(GLib.Error):
                self.connection.call_sync(
                    PORTAL_NAME,
                    self.session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                )
        self.session_handle = None
        self._requests.clear()
        if not remember:
            self.settings.set_enabled(False)
            self._status("Global shortcuts off", False)

    def close(self) -> None:
        self.stop(remember=True)
        if self.connection and self._response_subscription:
            self.connection.signal_unsubscribe(self._response_subscription)
        if self.connection and self._activated_subscription:
            self.connection.signal_unsubscribe(self._activated_subscription)
        self._response_subscription = 0
        self._activated_subscription = 0
