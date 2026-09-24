import unittest

from voiceyt.ui_theme import THEMES, get_theme


class ThemeTests(unittest.TestCase):
    def test_all_five_themes_have_semantic_palette(self) -> None:
        from voiceyt.ui_theme import THEMES
        self.assertEqual(set(THEMES), {"midnight", "ocean", "ember", "mono", "rose"})
        required = {"background", "surface", "surface_alt", "hover", "text", "text_dim", "accent", "danger", "focus"}
        for name, colors in THEMES.items():
            with self.subTest(name=name):
                self.assertTrue(required.issubset(colors))

    def test_unknown_theme_falls_back_to_midnight(self) -> None:
        from voiceyt.ui_theme import get_theme
        self.assertEqual(get_theme("does-not-exist"), get_theme("midnight"))
