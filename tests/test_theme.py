import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from link_studio.theme import _contrast_color, load_palette, palette_css


class ThemeTests(unittest.TestCase):
    def test_load_palette_and_generate_css(self):
        with TemporaryDirectory() as directory:
            colors = Path(directory) / "colors.toml"
            colors.write_text(
                "\n".join(
                    [
                        'mode = "dark"',
                        'accent = "#fb4f14"',
                        'selection = "#522315"',
                        'background = "#14100f"',
                        'foreground = "#ded2cf"',
                    ]
                )
            )
            palette = load_palette(colors, "gridiron")
            self.assertIsNotNone(palette)
            assert palette is not None
            self.assertEqual(palette.name, "gridiron")
            self.assertEqual(palette.accent, "#fb4f14")
            css = palette_css(palette)
            self.assertIn("@define-color accent_color #fb4f14", css)
            self.assertIn("@define-color window_bg_color #14100f", css)

    def test_invalid_colors_fall_back_safely(self):
        with TemporaryDirectory() as directory:
            colors = Path(directory) / "colors.toml"
            colors.write_text('accent = "red; } malicious {"\n')
            palette = load_palette(colors)
            self.assertIsNotNone(palette)
            assert palette is not None
            self.assertEqual(palette.accent, "#3584e4")

    def test_contrast_color(self):
        self.assertEqual(_contrast_color("#ffffff"), "#151515")
        self.assertEqual(_contrast_color("#000000"), "#ffffff")
