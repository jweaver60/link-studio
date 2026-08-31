import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from link_studio.shortcuts import SHORTCUTS, ShortcutSettings


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
