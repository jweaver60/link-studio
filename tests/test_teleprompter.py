import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from link_studio.teleprompter import ScriptStore


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


if __name__ == "__main__":
    unittest.main()
