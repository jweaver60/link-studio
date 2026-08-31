import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from link_studio.storage import StorageSettings


class StorageSettingsTests(unittest.TestCase):
    def test_output_directories_round_trip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = StorageSettings(root / "storage.json")
            settings.set_directory("screenshot", root / "shots")
            settings.set_directory("recording", root / "recordings")
            restored = StorageSettings(settings.path)
            self.assertEqual(restored.screenshot_directory, (root / "shots").resolve())
            self.assertEqual(restored.recording_directory, (root / "recordings").resolve())

    def test_unknown_directory_kind_is_rejected(self):
        with TemporaryDirectory() as directory:
            settings = StorageSettings(Path(directory) / "storage.json")
            with self.assertRaisesRegex(ValueError, "unsupported output"):
                settings.set_directory("cache", Path(directory))
