import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from link_studio.teleprompter import ScriptStore, TeleprompterWindow


class _Toggle:
    def __init__(self, active=True):
        self.active = active
        self.label = ""

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = active

    def set_label(self, label):
        self.label = label


class _Adjustment:
    def get_upper(self):
        return 100

    def get_page_size(self):
        return 10

    def get_value(self):
        return 89

    def set_value(self, _value):
        return None


class ScriptStoreTests(unittest.TestCase):
    def test_script_round_trip_and_update(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "scripts.json"
            store = ScriptStore(path)
            store.add("Intro", "Hello camera")
            store.update(0, "Opening", "Welcome to the show")

            restored = ScriptStore(path)
            self.assertEqual(restored.scripts[0].name, "Opening")
            self.assertEqual(restored.scripts[0].text, "Welcome to the show")

    def test_scripts_are_limited_and_sanitized(self):
        with TemporaryDirectory() as directory:
            store = ScriptStore(Path(directory) / "scripts.json")
            script = store.add("  Read me  ", "x" * (store.MAX_CHARACTERS + 10))
            self.assertEqual(script.name, "Read me")
            self.assertEqual(len(script.text), store.MAX_CHARACTERS)
            with self.assertRaisesRegex(ValueError, "name"):
                store.add("   ", "text")

    def test_play_can_restart_after_reaching_the_end(self):
        play = _Toggle()
        teleprompter = SimpleNamespace(
            _timer=73,
            _playing=True,
            play=play,
            scroll=SimpleNamespace(get_vadjustment=lambda: _Adjustment()),
            speed=SimpleNamespace(get_value=lambda: 60),
            loop=_Toggle(active=False),
            countdown_enabled=_Toggle(active=False),
            countdown_seconds=SimpleNamespace(get_value_as_int=lambda: 3),
            countdown_label=SimpleNamespace(
                set_label=lambda _label: None,
                set_visible=lambda _visible: None,
            ),
            _scroll_tick=lambda: True,
        )

        self.assertFalse(TeleprompterWindow._scroll_tick(teleprompter))
        self.assertEqual(teleprompter._timer, 0)
        self.assertFalse(play.get_active())

        play.set_active(True)
        with patch("link_studio.teleprompter.GLib.timeout_add", return_value=91):
            TeleprompterWindow._play_toggled(teleprompter, play)
        self.assertEqual(teleprompter._timer, 91)
        self.assertTrue(teleprompter._playing)


if __name__ == "__main__":
    unittest.main()
