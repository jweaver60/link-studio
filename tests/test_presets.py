import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from link_studio.presets import ColorPresetStore, PresetStore


class PresetStoreTests(unittest.TestCase):
    def test_preset_store_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "presets.json"
            store = PresetStore(path)
            store.add("Desk", {"mode": "deskview", "zoom": 125})

            restored = PresetStore(path)
            self.assertEqual(restored.presets[0].name, "Desk")
            self.assertEqual(restored.presets[0].values, {"mode": "deskview", "zoom": 125})
            self.assertEqual(json.loads(path.read_text())["presets"][0]["name"], "Desk")

    def test_legacy_list_is_migrated_and_default_is_persistent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "presets.json"
            path.write_text('[{"name":"Legacy","values":{"zoom":130}}]')
            store = PresetStore(path)
            self.assertEqual(store.presets[0].name, "Legacy")
            self.assertIsNone(store.default_index)

            store.rename(0, "Default desk")
            store.update(0, {"mode": "deskview", "zoom": 145})
            store.set_default(0)

            restored = PresetStore(path)
            self.assertEqual(restored.default_index, 0)
            self.assertEqual(restored.presets[0].name, "Default desk")
            self.assertEqual(restored.presets[0].values["zoom"], 145)

    def test_removing_a_scene_maintains_default_index(self):
        with TemporaryDirectory() as directory:
            store = PresetStore(Path(directory) / "presets.json")
            store.add("One", {})
            store.add("Two", {})
            store.add("Three", {})
            store.set_default(2)
            store.remove(0)
            self.assertEqual(store.default_index, 1)
            store.remove(1)
            self.assertIsNone(store.default_index)

    def test_preset_store_limit(self):
        with TemporaryDirectory() as directory:
            store = PresetStore(Path(directory) / "presets.json")
            for index in range(store.MAX_PRESETS):
                store.add(f"Scene {index}", {"zoom": 100 + index})
            with self.assertRaisesRegex(ValueError, "maximum"):
                store.add("One too many", {})

    def test_invalid_preset_file_is_ignored(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "presets.json"
            path.write_text("not json")
            self.assertEqual(PresetStore(path).presets, [])

    def test_color_presets_have_the_official_twenty_slot_limit(self):
        with TemporaryDirectory() as directory:
            store = ColorPresetStore(Path(directory) / "colors.json")
            self.assertEqual(store.MAX_PRESETS, 20)
            store.add("Interview", {"brightness": 7, "saturation": 62})
            self.assertEqual(ColorPresetStore(store.path).presets[0].name, "Interview")
