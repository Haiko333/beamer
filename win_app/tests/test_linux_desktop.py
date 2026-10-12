import unittest
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import linux_desktop


class DesktopTests(unittest.TestCase):
    def test_desktop_tokens_are_case_insensitive_and_colon_separated(self):
        for value in ("KDE", "kde", "KDE:Plasma", "plasma", "Something:KDE"):
            with self.subTest(value=value):
                self.assertEqual(linux_desktop.desktop({"XDG_CURRENT_DESKTOP": value}), "kde")
        for value in ("GNOME", "ubuntu:GNOME", "GNOME-Classic:GNOME"):
            with self.subTest(value=value):
                self.assertEqual(linux_desktop.desktop({"XDG_CURRENT_DESKTOP": value}), "gnome")

    def test_session_desktop_is_a_fallback_not_an_override(self):
        self.assertEqual(linux_desktop.desktop({"XDG_SESSION_DESKTOP": "KDE"}), "kde")
        self.assertEqual(linux_desktop.desktop({"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_DESKTOP": "KDE"}), "gnome")
        self.assertEqual(linux_desktop.desktop({"XDG_CURRENT_DESKTOP": "Xfce"}), "")
        self.assertEqual(linux_desktop.desktop({}), "")
        self.assertEqual(linux_desktop.desktop({"XDG_CURRENT_DESKTOP": "not-kde"}), "")


    def test_wlroots_compositors_are_named_by_the_desktop_or_by_their_own_socket(self):
        for environ, expected in (
            ({"XDG_CURRENT_DESKTOP": "Hyprland"}, "hyprland"),
            ({"HYPRLAND_INSTANCE_SIGNATURE": "abc_1_2"}, "hyprland"),
            ({"XDG_CURRENT_DESKTOP": "sway"}, "sway"),
            ({"XDG_CURRENT_DESKTOP": "sway:wlroots"}, "sway"),
            ({"SWAYSOCK": "/run/user/1000/sway-ipc.sock"}, "sway"),
            ({"XDG_CURRENT_DESKTOP": "wlroots"}, "wlroots"),
            ({"XDG_CURRENT_DESKTOP": "river"}, "wlroots"),
            ({"XDG_SESSION_DESKTOP": "labwc"}, "wlroots"),
            # GNOME and KDE keep their own answers whatever else the environment carries.
            ({"XDG_CURRENT_DESKTOP": "GNOME", "SWAYSOCK": "/stale"}, "gnome"),
        ):
            with self.subTest(environ=environ):
                self.assertEqual(linux_desktop.desktop(environ), expected)


class PortalProbeTests(unittest.TestCase):
    def test_no_session_bus_is_no_portal_and_no_error(self):
        with mock.patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/beamer-test-bus"}):
            self.assertEqual(linux_desktop.portals(timeout=0.2), frozenset())

    def test_each_interface_the_portal_answers_for_is_found_and_an_error_is_not(self):
        jeepney = SimpleNamespace(
            DBusAddress=lambda path, bus, interface: (path, bus, interface),
            MessageType=SimpleNamespace(error="error"),
            new_method_call=lambda address, method, signature, body: body[0],
        )
        closed = []

        class Connection:
            def send_and_get_reply(self, interface, timeout):
                kind = "error" if interface.endswith("RemoteDesktop") else "method_return"
                return SimpleNamespace(header=SimpleNamespace(message_type=kind))

            def close(self):
                closed.append(True)

        blocking = SimpleNamespace(open_dbus_connection=lambda bus: Connection())
        with mock.patch.dict(sys.modules, {"jeepney": jeepney, "jeepney.io": SimpleNamespace(blocking=blocking),
                                           "jeepney.io.blocking": blocking}):
            self.assertEqual(linux_desktop.portals(), frozenset({"InputCapture", "Clipboard"}))
        self.assertEqual(closed, [True])

    def test_a_portal_that_does_not_answer_in_time_is_unknown_not_absent(self):
        jeepney = SimpleNamespace(
            DBusAddress=lambda path, bus, interface: None,
            MessageType=SimpleNamespace(error="error"),
            new_method_call=lambda address, method, signature, body: body[0],
        )

        class Connection:
            def send_and_get_reply(self, interface, timeout):
                if not interface.endswith("InputCapture"):
                    raise TimeoutError()
                return SimpleNamespace(header=SimpleNamespace(message_type="method_return"))

            def close(self):
                pass

        blocking = SimpleNamespace(open_dbus_connection=lambda bus: Connection())
        with mock.patch.dict(sys.modules, {"jeepney": jeepney, "jeepney.io": SimpleNamespace(blocking=blocking),
                                           "jeepney.io.blocking": blocking}):
            self.assertIsNone(linux_desktop.portals())


class CrossingGuidanceTests(unittest.TestCase):
    def test_plasma_has_kwin_guidance_and_gnome_keeps_its_extension_note(self):
        from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
        import kvm_bridge_win as bridge

        app = QApplication.instance() or QApplication([])
        for name, shown, included, excluded in (
            ("KDE", True, "KWin", "AppIndicator"),
            ("ubuntu:GNOME", True, "AppIndicator", "KWin"),
            ("Xfce", False, "", "KWin"),
        ):
            with self.subTest(desktop=name), mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": name}), mock.patch.object(bridge.sys, "platform", "linux"):
                host = QWidget()
                layout = QVBoxLayout(host)
                view = SimpleNamespace(
                    _ways_module=lambda _: QWidget(), _pointer_module=lambda _: QWidget(), _resistance_module=lambda _: QWidget(),
                    _shortcut_module=lambda _: QWidget(), _reflect_ways=lambda: None, _reflect_other_name=lambda: None,
                )
                bridge.WindowsApplication._crossing_page(view, layout, bridge.default_config())
                self.assertEqual(not view.linux_tray_note.isHidden(), shown)
                self.assertIn(included, view.linux_tray_note.text())
                self.assertNotIn(excluded, view.linux_tray_note.text())
                host.deleteLater()


if __name__ == "__main__":
    unittest.main()
