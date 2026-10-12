"""What a layout types beyond Shift: the third and fourth levels AltGr reaches, and dead keys.

Both sides of a Linux machine read a layout through xkb_x11.Keymap, which answers one question
(what does this key give with these modifiers held). The capture asks it what the key under the
finger types with AltGr down, and what an accent and a letter make together; the injector asks it
the other way round, which keys to press for a character that Shift alone does not reach. xkb puts
the third level on Mod5 wherever a layout has one, and names the key that holds it
ISO_Level3_Shift: a layout whose right Alt is not that key (US) has no third level, and its right
Alt is plain Alt."""

from typing import Callable, Dict, List, Optional, Tuple
import unicodedata

from capture_x11 import KEYCODE_OFFSET, SHIFT_MASK

MOD5_MASK = 128
ISO_LEVEL3_SHIFT = 0xFE03
KEY_RIGHTALT, KEY_SPACE = 100, 57

# A dead key's keysym: (the combining mark it puts on the next letter, what it types on its own).
DEAD = {
    0xFE50: ("\u0300", "`"), 0xFE51: ("\u0301", "\u00b4"), 0xFE52: ("\u0302", "^"), 0xFE53: ("\u0303", "~"),
    0xFE54: ("\u0304", "\u00af"), 0xFE55: ("\u0306", "\u02d8"), 0xFE56: ("\u0307", "\u02d9"),
    0xFE57: ("\u0308", "\u00a8"), 0xFE58: ("\u030a", "\u00b0"), 0xFE59: ("\u030b", "\u02dd"),
    0xFE5A: ("\u030c", "\u02c7"), 0xFE5B: ("\u0327", "\u00b8"), 0xFE5C: ("\u0328", "\u02db"),
}
_BY_MARK = {mark: keysym for keysym, (mark, _alone) in DEAD.items()}
_BY_ALONE = {alone: keysym for keysym, (_mark, alone) in DEAD.items()}

# One key down and up: (keycode, with Shift, with AltGr).
Stroke = Tuple[int, bool, bool]


def has_third_level(keymap, group: int) -> bool:
    """Whether right Alt is AltGr in this layout."""
    return keymap.keysym(KEY_RIGHTALT + KEYCODE_OFFSET, 0, group) == ISO_LEVEL3_SHIFT


def compose(dead: int, character: str) -> str:
    """What the dead key `dead` and then `character` type: the accented letter, the accent alone
    before a space or itself, and both as they stand where Unicode has no such letter."""
    mark, alone = DEAD[dead]
    if character in (" ", alone):
        return alone
    composed = unicodedata.normalize("NFC", character + mark)
    return composed if len(composed) == 1 else alone + character


class Levels:
    """The keys of a layout by what they type at every level, for typing a character Shift alone
    does not reach. Tables are made once for each group and state of the locks."""

    def __init__(self, keymap, usable: Optional[Callable[[int], bool]] = None) -> None:
        self.keymap = keymap
        # Which keycodes can be pressed at all: a layout names keys no keyboard has.
        self._usable = usable or (lambda keycode: True)
        self._tables: Dict[Tuple[int, int], Tuple[Dict[str, Stroke], Dict[int, Stroke]]] = {}

    def _table(self, group: int, locks: int) -> Tuple[Dict[str, Stroke], Dict[int, Stroke]]:
        table = self._tables.get((group, locks))
        if table is None:
            keymap = self.keymap
            characters: Dict[str, Stroke] = {}
            dead: Dict[int, Stroke] = {}
            # The level a finger reaches most easily is written last and so kept.
            levels = [(True, True), (False, True)] if has_third_level(keymap, group) else []
            for shift, altgr in levels + [(True, False), (False, False)]:
                mods = locks | (SHIFT_MASK if shift else 0) | (MOD5_MASK if altgr else 0)
                for keycode in range(keymap.max_keycode, keymap.min_keycode - 1, -1):
                    if not self._usable(keycode):
                        continue
                    keysym = keymap.keysym(keycode, mods, group)
                    if keysym in DEAD:
                        dead[keysym] = (keycode, shift, altgr)
                    elif altgr:
                        text = keymap.text(keysym)
                        if text:
                            characters[text] = (keycode, shift, altgr)
            table = self._tables[(group, locks)] = (characters, dead)
        return table

    def strokes(self, character: str, group: int, locks: int,
                find: Callable[[str], Optional[Tuple[int, bool]]]) -> Optional[List[Stroke]]:
        """The keys typing `character`, which `find` (the first two levels) has no key for: one
        with AltGr, or a dead key and then a letter or the space bar. None when nothing does."""
        characters, dead = self._table(group, locks)

        def one(wanted: str) -> Optional[Stroke]:
            plain = find(wanted)
            if plain is not None:
                return plain[0], plain[1], False
            return characters.get(wanted)

        direct = characters.get(character)
        if direct is not None:
            return [direct]
        accent = dead.get(_BY_ALONE.get(character))
        if accent is not None:
            return [accent, (KEY_SPACE + KEYCODE_OFFSET, False, False)]
        parts = unicodedata.normalize("NFD", character)
        if len(parts) == 2:
            accent, letter = dead.get(_BY_MARK.get(parts[1])), one(parts[0])
            if accent is not None and letter is not None:
                return [accent, letter]
        return None
