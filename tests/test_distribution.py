import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ID = "io.github.linkstudio.LinkStudio"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


class DistributionAssetsTests(unittest.TestCase):
    def test_desktop_entry_uses_application_icon(self) -> None:
        desktop = (ROOT / "data" / f"{APP_ID}.desktop").read_text(encoding="utf-8")
        self.assertIn(f"Icon={APP_ID}\n", desktop)

    def test_hicolor_icons_are_rgba_pngs_at_their_declared_sizes(self) -> None:
        for size in ICON_SIZES:
            with self.subTest(size=size):
                path = (
                    ROOT
                    / "data"
                    / "icons"
                    / "hicolor"
                    / f"{size}x{size}"
                    / "apps"
                    / f"{APP_ID}.png"
                )
                contents = path.read_bytes()
                self.assertEqual(contents[:8], b"\x89PNG\r\n\x1a\n")
                width, height = struct.unpack(">II", contents[16:24])
                self.assertEqual((width, height), (size, size))
                self.assertEqual(contents[25], 6, "icon must retain an alpha channel")


class LauncherScriptTests(unittest.TestCase):
    def test_install_user_rejects_incompatible_python_before_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            mock_python = tmp_path / "mock_python"
            mock_python.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ]; then\n'
                '  case "$2" in\n'
                '    *"sys.version_info >= (3, 12)"*) exit 1 ;;\n'
                '    *"sys.version_info.major"*) echo "3.10.12"; exit 0 ;;\n'
                '    *"import gi"*) exit 0 ;;\n'
                "  esac\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            mock_python.chmod(0o755)

            data_root = tmp_path / "data"
            env = dict(
                os.environ,
                LINK_STUDIO_PYTHON=str(mock_python),
                XDG_DATA_HOME=str(data_root),
                XDG_BIN_HOME=str(tmp_path / "bin"),
            )
            proc = subprocess.run(
                [str(ROOT / "scripts" / "install-user")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Link Studio requires Python >= 3.12", proc.stderr)
            self.assertIn("found Python 3.10.12", proc.stderr)
            self.assertFalse((data_root / "link-studio" / "venv").exists())

    def test_run_dev_rejects_incompatible_python_before_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            mock_python = tmp_path / "mock_python"
            mock_python.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ]; then\n'
                '  case "$2" in\n'
                '    *"sys.version_info >= (3, 12)"*) exit 1 ;;\n'
                '    *"sys.version_info.major"*) echo "3.10.12"; exit 0 ;;\n'
                '    *"import gi"*) exit 0 ;;\n'
                "  esac\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            mock_python.chmod(0o755)

            env = dict(os.environ, LINK_STUDIO_PYTHON=str(mock_python))
            proc = subprocess.run(
                [str(ROOT / "scripts" / "run-dev")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("Link Studio requires Python >= 3.12", proc.stderr)
            self.assertIn("found Python 3.10.12", proc.stderr)

    def test_install_user_rejects_missing_gi_before_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            mock_python = tmp_path / "mock_python"
            mock_python.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ]; then\n'
                '  case "$2" in\n'
                '    *"sys.version_info >= (3, 12)"*) exit 0 ;;\n'
                '    *"import gi"*) exit 1 ;;\n'
                "  esac\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            mock_python.chmod(0o755)

            data_root = tmp_path / "data"
            env = dict(
                os.environ,
                LINK_STUDIO_PYTHON=str(mock_python),
                XDG_DATA_HOME=str(data_root),
                XDG_BIN_HOME=str(tmp_path / "bin"),
            )
            proc = subprocess.run(
                [str(ROOT / "scripts" / "install-user")],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn(
                "Link Studio requires the system GTK bindings (python-gobject / gi)",
                proc.stderr,
            )
            self.assertFalse((data_root / "link-studio" / "venv").exists())


if __name__ == "__main__":
    unittest.main()
