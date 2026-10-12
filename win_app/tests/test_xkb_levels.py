"""What a dead key and the next character type, and which layouts have a third level: over
libxkbcommon's own layouts where one is needed, with no keyboard."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xkb_levels

CIRCUMFLEX, DIAERESIS, CEDILLA = 0xFE52, 0xFE57, 0xFE5B


class ComposeTest(unittest.TestCase):
    def test_an_accent_and_a_letter_it_goes_on_are_one_character(self):
        self.assertEqual([xkb_levels.compose(CIRCUMFLEX, "e"), xkb_levels.compose(DIAERESIS, "I"), xkb_levels.compose(CEDILLA, "c")],
                         ["ê", "Ï", "ç"])

    def test_before_a_space_or_itself_the_accent_stands_alone(self):
        self.assertEqual([xkb_levels.compose(CIRCUMFLEX, " "), xkb_levels.compose(CIRCUMFLEX, "^")], ["^", "^"])

    def test_before_a_letter_it_does_not_go_on_both_are_typed(self):
        self.assertEqual(xkb_levels.compose(CIRCUMFLEX, "x"), "^x")

    def test_every_dead_key_names_a_combining_mark_and_one_character_alone(self):
        import unicodedata

        for keysym, (mark, alone) in xkb_levels.DEAD.items():
            with self.subTest(keysym=hex(keysym)):
                self.assertTrue(unicodedata.combining(mark))
                self.assertEqual(len(alone), 1)


class LayoutTest(unittest.TestCase):
    def keymap(self, layout):
        try:
            import xkb_x11

            keymap = xkb_x11.Keymap.from_names(layout)
        except (RuntimeError, OSError) as exc:
            self.skipTest(f"needs libxkbcommon and the {layout} layout: {exc}")
        self.addCleanup(keymap.close)
        return keymap

    def test_right_alt_is_altgr_on_azerty_and_qwertz_and_plain_alt_on_us(self):
        self.assertEqual([xkb_levels.has_third_level(self.keymap(layout), 0) for layout in ("fr", "de", "us")], [True, True, False])

    def test_the_dead_keys_table_names_the_keysyms_xkb_gives(self):
        keymap = self.keymap("fr")
        # The key right of P: circumflex, and the diaeresis with Shift.
        self.assertEqual([keymap.keysym(26 + 8, mods, 0) for mods in (0, 1)], [CIRCUMFLEX, DIAERESIS])

    def test_a_layout_with_no_third_level_has_nothing_beyond_shift(self):
        levels = xkb_levels.Levels(self.keymap("us"))
        self.assertIsNone(levels.strokes("€", 0, 0, lambda wanted: None))


if __name__ == "__main__":
    unittest.main()
