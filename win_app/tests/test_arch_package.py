"""The Arch package (packaging/arch): it installs files that exist, depends on what
requirements-linux.txt asks for, and leaves both udev rules as examples to turn on by hand."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCH = ROOT / "packaging" / "arch"

# requirements-linux.txt's names as Arch has them.
ARCH_NAMES = {
    "PySide6-Essentials": "pyside6", "PyNaCl": "python-pynacl", "python-xlib": "python-xlib",
    "six": "python-six", "jeepney": "python-jeepney", "evdev": "python-evdev",
}


def pkgbuild() -> str:
    return (ARCH / "PKGBUILD").read_text(encoding="utf-8")


def array(name: str) -> list:
    body = re.search(rf"^{name}=\((.*?)\)", pkgbuild(), re.S | re.M).group(1)
    return [word.split(":")[0] for word in re.findall(r"'([^']+)'", body)]


def installs() -> list:
    """(source, destination) of every `install -D` line in package()."""
    return re.findall(r'^\s*install -Dm\d+ (\S+) "\$pkgdir(\S+)"', pkgbuild(), re.M)


class ArchPackageTest(unittest.TestCase):
    def test_every_file_it_installs_is_in_the_repository(self):
        found = installs()
        self.assertTrue(found)
        for source, _ in found:
            self.assertTrue((ROOT / source).is_file(), source)

    def test_it_depends_on_everything_the_linux_requirements_name(self):
        depends = array("depends")
        lines = (ROOT / "win_app" / "requirements-linux.txt").read_text(encoding="utf-8").splitlines()
        names = [re.split(r"[=<>]", line)[0].strip() for line in lines if line.strip() and not line.startswith("#")]
        self.assertEqual(sorted(names), sorted(ARCH_NAMES))
        for name in names:
            self.assertIn(ARCH_NAMES[name], depends)

    def test_neither_udev_rule_is_in_force(self):
        places = dict(installs())
        for rule in ("70-beamer-uinput.rules", "70-beamer-input.rules"):
            place = places["packaging/linux/" + rule]
            self.assertNotIn("rules.d", place)
            self.assertIn("/usr/share/doc/", place)

    def test_the_launcher_starts_the_shell_where_the_package_puts_it(self):
        launcher = (ROOT / "packaging" / "linux" / "beamer").read_text(encoding="utf-8")
        self.assertIn("/usr/share/beamer/win_app/kvm_bridge_win.py", launcher)
        self.assertIn('cp -r core win_app VERSION "$pkgdir/usr/share/beamer/"', pkgbuild())

    def test_the_note_shown_after_installing_says_what_each_rule_opens_and_how_to_take_it_back(self):
        note = (ARCH / "beamer.install").read_text(encoding="utf-8")
        for rule in ("70-beamer-uinput.rules", "70-beamer-input.rules"):
            self.assertIn(f"sudo install -m 644 /usr/share/doc/beamer/{rule} /etc/udev/rules.d/", note)
            self.assertIn(f"sudo rm /etc/udev/rules.d/{rule}", note)
        self.assertIn("type and click as you", note)
        self.assertIn("passwords", note)


if __name__ == "__main__":
    unittest.main()
