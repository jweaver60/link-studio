import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from link_studio.diagnostics import create_support_bundle


class DiagnosticsTests(unittest.TestCase):
    def test_support_bundle_contains_only_webcam_runtime_diagnostics(self):
        camera = SimpleNamespace(device=SimpleNamespace(path="/dev/video0"))
        commands = []

        def command(arguments):
            commands.append(arguments)
            return "ok\n"

        with TemporaryDirectory() as directory:
            with (
                patch("link_studio.diagnostics.diagnostic_report", return_value={"ok": True}),
                patch("link_studio.diagnostics._command", side_effect=command),
                patch(
                    "link_studio.diagnostics.log_path",
                    return_value=Path(directory) / "missing.log",
                ),
            ):
                bundle = create_support_bundle(camera, Path(directory))
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())

        self.assertNotIn("orca-runtime.json", names)
        self.assertFalse(any(arguments[0] == "orca-ide" for arguments in commands))
