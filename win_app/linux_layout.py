"""Which keyboard layout this machine types with, for the evdev back ends, which press and read
keys below the compositor and so must work out for themselves what each key types.

The portal back ends are handed the compositor's own keymap; nothing hands one over here. What is
known is the layout's XKB names ("fr", "de" with variant "neo"), and libxkbcommon compiles from
those the same keymap the compositor compiled. The names are, in order:

- BEAMER_XKB_LAYOUT, as "fr" or "fr(bepo)", for a desktop that tells Beamer nothing or the wrong thing;
- what the compositor says: Hyprland gives the names themselves, sway only what people read
  ("French (AZERTY)"), which XKB's own list of layouts turns back into names;
- XKB_DEFAULT_LAYOUT and its siblings, which a wlroots compositor with no layout configured uses;
- the system's console and X11 layout, as localectl set it;
- the system default, which is "us".

A layout that cannot be found falls through to the next source and is logged once."""

import functools
import logging
import os
import re
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Tuple

LOGGER = logging.getLogger(__name__)

XKB_LIST = ("/usr/share/X11/xkb/rules/evdev.xml", "/usr/share/xkeyboard-config-2/rules/evdev.xml")
SYSTEM_FILES = ("/etc/X11/xorg.conf.d/00-keyboard.conf", "/etc/default/keyboard")


class Names(NamedTuple):
    layout: str = ""
    variant: str = ""
    options: str = ""
    model: str = ""
    rules: str = ""


def parse_override(text: str) -> Optional[Names]:
    """"fr", "fr(bepo)" or "fr,us(intl)": XKB's own way of writing a layout with its variant."""
    layouts, variants = [], []
    for part in text.split(","):
        match = re.fullmatch(r"\s*([A-Za-z0-9_]+)\s*(?:\(\s*([A-Za-z0-9_.-]*)\s*\))?\s*", part)
        if match is None:
            return None
        layouts.append(match.group(1))
        variants.append(match.group(2) or "")
    return Names(",".join(layouts), ",".join(variants) if any(variants) else "")


@functools.lru_cache(maxsize=None)
def descriptions(paths=XKB_LIST) -> Dict[str, Tuple[str, str]]:
    """XKB's list of layouts, by what people read: "French (AZERTY)" to ("fr", "azerty"). Read
    once: it is asked for at every character typed on sway."""
    from xml.etree import ElementTree

    for path in paths:
        try:
            root = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        found: Dict[str, Tuple[str, str]] = {}
        for layout in root.iter("layout"):
            name = layout.findtext("configItem/name")
            if not name:
                continue
            found.setdefault(layout.findtext("configItem/description") or "", (name, ""))
            for variant in layout.iter("variant"):
                found.setdefault(variant.findtext("configItem/description") or "",
                                 (name, variant.findtext("configItem/name") or ""))
        found.pop("", None)
        return found
    return {}


def from_descriptions(read: list, known: Dict[str, Tuple[str, str]]) -> Optional[Names]:
    """sway's layout names as XKB's; None unless every one is found, since the groups must line up."""
    pairs = [known.get(text) for text in read]
    if not pairs or any(pair is None for pair in pairs):
        return None
    variants = [pair[1] for pair in pairs]
    return Names(",".join(pair[0] for pair in pairs), ",".join(variants) if any(variants) else "")


def from_system(paths=SYSTEM_FILES) -> Optional[Names]:
    """The layout localectl wrote for X11 (XkbLayout in xorg.conf.d) or Debian's XKBLAYOUT."""
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        values = {}
        for key, xorg, debian in (("layout", "XkbLayout", "XKBLAYOUT"), ("variant", "XkbVariant", "XKBVARIANT"),
                                  ("options", "XkbOptions", "XKBOPTIONS"), ("model", "XkbModel", "XKBMODEL")):
            match = (re.search(rf'^\s*Option\s+"{xorg}"\s+"([^"]*)"', text, re.MULTILINE)
                     or re.search(rf'^\s*{debian}\s*=\s*"?([^"\n]*)"?', text, re.MULTILINE))
            values[key] = match.group(1).strip() if match else ""
        if values["layout"]:
            return Names(**values)
    return None


def sources(environ=None, said: Optional[dict] = None, known=descriptions, system=from_system):
    """Every (where from, Names) there is, best first. `said` is what the compositor says
    (desktop_wlroots.keyboard)."""
    environ = os.environ if environ is None else environ
    override = environ.get("BEAMER_XKB_LAYOUT", "").strip()
    if override:
        parsed = parse_override(override)
        if parsed is not None:
            yield "BEAMER_XKB_LAYOUT", parsed
        else:
            LOGGER.warning("BEAMER_XKB_LAYOUT=%r is not a layout as XKB writes one, like fr or fr(bepo)", override)
    if said:
        if said.get("layout"):
            yield "the compositor", Names(*(said.get(field) or "" for field in Names._fields))
        elif said.get("descriptions"):
            named = from_descriptions(said["descriptions"], known())
            if named is not None:
                yield "the compositor", named
    if environ.get("XKB_DEFAULT_LAYOUT"):
        yield "XKB_DEFAULT_LAYOUT", Names(*(environ.get(f"XKB_DEFAULT_{field.upper()}", "") for field in Names._fields))
    found = system()
    if found is not None:
        yield "the system's keyboard settings", found
    yield "the system default", Names()


def keymap(environ=None, said: Optional[dict] = None, compile_=None, **seams):
    """(xkb_x11.Keymap, Names, where from) for the first source whose layout this machine has."""
    if compile_ is None:
        import xkb_x11

        compile_ = xkb_x11.Keymap.from_names
    failure = None
    for origin, names in sources(environ, said, **seams):
        try:
            return compile_(*names), names, origin
        except Exception as exc:
            failure = exc
            LOGGER.warning("The keyboard layout from %s cannot be used (%s)", origin, exc)
    raise RuntimeError(f"No keyboard layout could be read: {failure}")
