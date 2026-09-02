import struct
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


if __name__ == "__main__":
    unittest.main()
