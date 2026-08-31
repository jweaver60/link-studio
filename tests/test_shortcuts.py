import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from link_studio.constants import APP_ID
from link_studio.shortcuts import (
    HOST_REGISTRY_INTERFACE,
    PORTAL_INTERFACE,
    PORTAL_NAME,
    PORTAL_PATH,
    SHORTCUTS,
    GlobalShortcutPortal,
    ShortcutSettings,
)


class ShortcutTests(unittest.TestCase):
    def test_global_shortcut_ids_are_unique(self):
        identifiers = [shortcut.identifier for shortcut in SHORTCUTS]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertIn("tracking", identifiers)
        self.assertIn("privacy", identifiers)
        self.assertIn("screenshot", identifiers)

    def test_enabled_state_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "shortcuts.json"
            settings = ShortcutSettings(path)
            self.assertFalse(settings.enabled)
            settings.set_enabled(True)
            self.assertTrue(ShortcutSettings(path).enabled)
            self.assertTrue(json.loads(path.read_text())["enabled"])

    def test_portal_identity_is_registered_before_global_shortcut_proxy(self):
        connection = Mock()
        connection.signal_subscribe.side_effect = (1, 2)
        proxy = Mock()
        events = []

        def register(*_args):
            events.append("register")

        connection.call_sync.side_effect = register

        def create_proxy(*_args):
            events.append("proxy")
            return proxy

        portal = GlobalShortcutPortal(Mock())
        with (
            patch("link_studio.shortcuts.Gio.bus_get_sync", return_value=connection),
            patch("link_studio.shortcuts.Gio.DBusProxy.new_sync", side_effect=create_proxy),
        ):
            self.assertIs(portal._ensure_proxy(), proxy)

        self.assertEqual(events, ["register", "proxy"])
        registration = connection.call_sync.call_args.args
        self.assertEqual(
            registration[:4],
            (PORTAL_NAME, PORTAL_PATH, HOST_REGISTRY_INTERFACE, "Register"),
        )
        self.assertEqual(registration[4].unpack(), (APP_ID, {}))
        self.assertIs(portal.proxy, proxy)
        self.assertEqual(PORTAL_INTERFACE, "org.freedesktop.portal.GlobalShortcuts")
