"""Which keyboard layout the evdev back ends type with: the order of the sources, and each one's
reading. Nothing here needs libxkbcommon or a compositor."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import linux_layout
from linux_layout import Names

KNOWN = {"French": ("fr", ""), "French (AZERTY)": ("fr", "azerty"), "English (US)": ("us", "")}


def origins(environ, said=None, system=lambda: None):
    return [(origin, names.layout, names.variant) for origin, names in
            linux_layout.sources(environ, said, known=lambda: KNOWN, system=system)]


class SourcesTest(unittest.TestCase):
    def test_the_order_is_override_compositor_environment_system_default(self):
        environ = {"BEAMER_XKB_LAYOUT": "de(neo)", "XKB_DEFAULT_LAYOUT": "es", "XKB_DEFAULT_VARIANT": "cat"}
        found = origins(environ, {"layout": "fr", "variant": "bepo", "group": 0}, lambda: Names("it"))
        self.assertEqual(found, [("BEAMER_XKB_LAYOUT", "de", "neo"), ("the compositor", "fr", "bepo"),
                                 ("XKB_DEFAULT_LAYOUT", "es", "cat"), ("the system's keyboard settings", "it", ""),
                                 ("the system default", "", "")])

    def test_with_nothing_said_the_system_default_stands(self):
        self.assertEqual(origins({}), [("the system default", "", "")])

    def test_sways_descriptions_become_xkb_names_only_when_every_one_is_known(self):
        said = {"descriptions": ["French (AZERTY)", "English (US)"], "group": 1}
        self.assertEqual(origins({}, said)[0], ("the compositor", "fr,us", "azerty,"))
        self.assertEqual(origins({}, {"descriptions": ["French", "Klingon"]}), [("the system default", "", "")])

    def test_an_override_that_is_not_a_layout_is_passed_over(self):
        linux_layout._warned.clear()
        with self.assertLogs(linux_layout.LOGGER, "WARNING"):
            self.assertEqual(origins({"BEAMER_XKB_LAYOUT": "fr; rm -rf"}), [("the system default", "", "")])

    def test_a_name_xkb_could_not_have_is_passed_over_whoever_says_it(self):
        linux_layout._warned.clear()
        with self.assertLogs(linux_layout.LOGGER, "WARNING") as logged:
            for _ in range(3):
                found = origins({"XKB_DEFAULT_LAYOUT": "../../etc/passwd"}, {"layout": "fr/../x"}, lambda: linux_layout.Names("us"))
        self.assertEqual(found, [("the system's keyboard settings", "us", ""), ("the system default", "", "")])
        self.assertEqual(len(logged.records), 2)  # once for each source, not at every character

    def test_override_spellings(self):
        self.assertEqual(linux_layout.parse_override("fr"), Names("fr"))
        self.assertEqual(linux_layout.parse_override(" fr(bepo) , us "), Names("fr,us", "bepo,"))
        self.assertIsNone(linux_layout.parse_override("fr("))


class FilesTest(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
        self.addCleanup(lambda: Path(handle.name).unlink())
        handle.write(text)
        handle.close()
        return handle.name

    def test_localectls_xorg_file_and_debians_default_keyboard(self):
        xorg = self.write('Section "InputClass"\n        Option "XkbLayout" "fr"\n        Option "XkbModel" "pc105"\n'
                          '        Option "XkbOptions" "terminate:ctrl_alt_bksp"\nEndSection\n')
        self.assertEqual(linux_layout.from_system((xorg,)), Names("fr", "", "terminate:ctrl_alt_bksp", "pc105"))
        debian = self.write('XKBMODEL="pc105"\nXKBLAYOUT="de"\nXKBVARIANT="nodeadkeys"\nXKBOPTIONS=""\n')
        self.assertEqual(linux_layout.from_system(("/nonexistent", debian)), Names("de", "nodeadkeys", "", "pc105"))
        self.assertIsNone(linux_layout.from_system(("/nonexistent",)))

    def test_xkbs_list_gives_each_description_its_names(self):
        listing = self.write(
            "<xkbConfigRegistry><layoutList><layout><configItem><name>fr</name><description>French</description></configItem>"
            "<variantList><variant><configItem><name>azerty</name><description>French (AZERTY)</description></configItem></variant>"
            "</variantList></layout></layoutList></xkbConfigRegistry>")
        self.assertEqual(linux_layout.descriptions((listing,)), {"French": ("fr", ""), "French (AZERTY)": ("fr", "azerty")})
        self.assertEqual(linux_layout.descriptions(("/nonexistent/evdev.xml",)), {})


class KeymapTest(unittest.TestCase):
    def test_a_layout_this_machine_does_not_have_falls_through_to_the_next_source(self):
        tried = []

        def compile_(*names):
            tried.append(names[0])
            if names[0] == "zz":
                raise RuntimeError("no such layout")
            return "keymap"

        with self.assertLogs(linux_layout.LOGGER, "WARNING"):
            found = linux_layout.keymap({"BEAMER_XKB_LAYOUT": "zz"}, {"layout": "fr"}, compile_, system=lambda: None)
        self.assertEqual((found, tried), (("keymap", Names("fr"), "the compositor"), ["zz", "fr"]))

    def test_no_layout_at_all_is_an_error_with_the_reason(self):
        def compile_(*names):
            raise RuntimeError("libxkbcommon is not installed")

        with self.assertLogs(linux_layout.LOGGER, "WARNING"), self.assertRaisesRegex(RuntimeError, "libxkbcommon"):
            linux_layout.keymap({}, None, compile_, system=lambda: None)


if __name__ == "__main__":
    unittest.main()
