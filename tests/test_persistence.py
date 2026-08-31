import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from link_studio.presets import PresetStore
from link_studio.shortcuts import ShortcutSettings
from link_studio.storage import StorageSettings
from link_studio.teleprompter import ScriptStore


class Utf8PersistenceTests(unittest.TestCase):
    def test_non_ascii_local_data_round_trips_as_utf8(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            presets = PresetStore(root / "presets.json")
            presets.add("Café résumé", {"zoom": 125})
            scripts = ScriptStore(root / "scripts.json")
            scripts.add("日本語", "Zażółć gęślą jaźń")

            self.assertEqual(PresetStore(presets.path).presets[0].name, "Café résumé")
            self.assertEqual(ScriptStore(scripts.path).scripts[0].text, "Zażółć gęślą jaźń")

    def test_invalid_utf8_files_degrade_to_defaults(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            constructors = (
                PresetStore,
                ScriptStore,
                ShortcutSettings,
                StorageSettings,
            )
            for index, constructor in enumerate(constructors):
                path = root / f"invalid-{index}.json"
                path.write_bytes(b"\xff\xfe\x00")
                instance = constructor(path)
                self.assertIsNotNone(instance)


if __name__ == "__main__":
    unittest.main()
