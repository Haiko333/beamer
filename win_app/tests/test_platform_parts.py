"""Which Linux back ends are chosen: by the session, by what the desktop's portal offers, and by
BEAMER_BACKEND. No bus is reached: the portal's answer is passed in."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import platform_parts

GNOME = frozenset({"InputCapture", "RemoteDesktop", "Clipboard"})
HYPRLAND = frozenset({"InputCapture"})


def never():
    raise AssertionError("the portal was asked")


class BackendTest(unittest.TestCase):
    def test_x11_never_asks_the_portal(self):
        self.assertEqual(platform_parts.backend({"XDG_SESSION_TYPE": "x11", "BEAMER_BACKEND": "evdev"}, never), "x11")

    def test_a_desktop_that_can_be_driven_through_the_portal_uses_it(self):
        self.assertEqual(platform_parts.backend({"XDG_SESSION_TYPE": "wayland"}, lambda: GNOME), "portal")
        # An older GNOME: no InputCapture, but it is still driven through RemoteDesktop.
        self.assertEqual(platform_parts.backend({"XDG_SESSION_TYPE": "wayland"}, lambda: frozenset({"RemoteDesktop"})), "portal")

    def test_a_desktop_without_remote_desktop_or_without_any_portal_gets_evdev(self):
        self.assertEqual(platform_parts.backend({"XDG_SESSION_TYPE": "wayland"}, lambda: HYPRLAND), "evdev")
        self.assertEqual(platform_parts.backend({"XDG_SESSION_TYPE": "wayland"}, frozenset), "evdev")

    def test_a_portal_too_slow_to_answer_does_not_take_gnome_or_kde_off_it(self):
        for desktop, expected in (("GNOME", "portal"), ("KDE", "portal"), ("Hyprland", "evdev"), ("", "evdev")):
            with self.subTest(desktop=desktop):
                environ = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": desktop}
                self.assertEqual(platform_parts.backend(environ, lambda: None), expected)

    def test_the_environment_names_a_wayland_backend_without_asking(self):
        for forced in ("portal", "evdev", " EVDEV "):
            with self.subTest(forced=forced):
                environ = {"XDG_SESSION_TYPE": "wayland", "BEAMER_BACKEND": forced}
                self.assertEqual(platform_parts.backend(environ, never), forced.strip().lower())

    def test_an_unknown_name_is_ignored(self):
        environ = {"XDG_SESSION_TYPE": "wayland", "BEAMER_BACKEND": "x11"}
        self.assertEqual(platform_parts.backend(environ, lambda: GNOME), "portal")


class LoadTest(unittest.TestCase):
    def test_every_linux_set_has_every_part(self):
        for environ, portals, backend in (
            ({"XDG_SESSION_TYPE": "x11"}, never, "x11"),
            ({"XDG_SESSION_TYPE": "wayland"}, lambda: GNOME, "portal"),
            ({"XDG_SESSION_TYPE": "wayland"}, lambda: HYPRLAND, "evdev"),
        ):
            with self.subTest(backend=backend):
                parts = platform_parts.load("linux", environ, portals)
                self.assertEqual(parts.backend, backend)
                for part in platform_parts.PARTS:
                    self.assertTrue(hasattr(parts, part), part)

    def test_the_sets_are_whole(self):
        portal = platform_parts.load("linux", {"XDG_SESSION_TYPE": "wayland"}, lambda: GNOME)
        self.assertEqual([portal.capture.__name__, portal.injector.__name__, portal.clipboard.__name__, portal.desktop.__name__],
                         ["capture_portal", "inject_portal", "clipboard_portal", "desktop_portal"])
        evdev = platform_parts.load("linux", {"XDG_SESSION_TYPE": "wayland"}, lambda: HYPRLAND)
        self.assertEqual([evdev.capture.__name__, evdev.injector.__name__, evdev.clipboard.__name__, evdev.desktop.__name__],
                         ["capture_libinput", "inject_uinput", "clipboard_wl", "desktop_wlroots"])

    def test_every_capture_part_has_what_the_shell_and_the_settings_read(self):
        for portals in (lambda: GNOME, lambda: HYPRLAND):
            capture = platform_parts.load("linux", {"XDG_SESSION_TYPE": "wayland"}, portals).capture
            for name in ("Hooks", "Trigger", "TOGGLE", "REDIRECT", "mouse_event", "hook_vk", "input_title",
                         "VK_TO_NAME", "NOT_TRIGGER_VKS", "VK_TITLES", "UNRECORDABLE_TRIGGER_VKS"):
                with self.subTest(capture=capture.__name__, name=name):
                    self.assertTrue(hasattr(capture, name))


if __name__ == "__main__":
    unittest.main()
