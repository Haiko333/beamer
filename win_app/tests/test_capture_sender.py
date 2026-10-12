"""Capture events pass through the Qt shell to the real sender, without a desktop."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capture_win
import kvm_bridge_win
import platform_parts
import sender
from core import protocol
from links_rig import B, Rig


class Keymap:
    def keysym(self, keycode, mods, group):
        return ord("a") if keycode == 38 else 0

    def text(self, keysym):
        return chr(keysym) if keysym else None

    def core_mods(self, mods):
        return mods


def shell(rig, capture):
    return SimpleNamespace(sender=rig.sender, _trigger=capture.Trigger())


def check_linux_key(capture, session, code, name, us):
    rig = Rig()
    assert rig.sender.set_redirecting(True)
    application = shell(rig, capture)
    hooks = capture.Hooks(
        lambda *args: kvm_bridge_win.WindowsApplication._on_hook_key(application, *args),
        lambda *_: False,
        lambda *_: None,
    )
    if session == "x11":
        hooks._connection = SimpleNamespace(keymap=Keymap())

        def feed(down):
            hooks._key(code + 8, down, 0, 0, True)
    elif session == "evdev":
        hooks._keymap_factory = lambda said=None: (Keymap(), None, "a test")

        def feed(down):
            hooks._key(1, code, down)
    else:
        hooks._keymap = Keymap()
        device = object()

        def feed(down):
            hooks._key_event(code, down, device)

    feed(True)
    feed(False)
    expected = {"key": name}
    if us is not None:
        expected["us"] = us
    assert rig.sent(B, protocol.MSG_KEYDOWN) == [{"type": protocol.MSG_KEYDOWN, "data": expected}]
    assert rig.sent(B, protocol.MSG_KEYUP) == [{"type": protocol.MSG_KEYUP, "data": expected}]


class CaptureSenderTests(unittest.TestCase):
    def check_backend(self, session):
        environ = {"XDG_SESSION_TYPE": "wayland", "BEAMER_BACKEND": "evdev"} if session == "evdev" else {
            "XDG_SESSION_TYPE": session, "BEAMER_BACKEND": "portal"}
        capture = platform_parts.load("linux", environ).capture
        with mock.patch.object(kvm_bridge_win, "capture_win", capture), mock.patch.object(sender, "OWN_PLATFORM", "linux"):
            for code, name, us in ((30, "a", "a"), (28, "enter", None), (42, "shift", None)):
                with self.subTest(key=name):
                    check_linux_key(capture, session, code, name, us)

    def test_x11_capture_key_press_and_release_reach_sender(self):
        self.check_backend("x11")

    def test_wayland_capture_key_press_and_release_reach_sender(self):
        self.check_backend("wayland")

    def test_evdev_capture_key_press_and_release_reach_sender(self):
        import desktop_wlroots
        import linux_layout

        desktop_wlroots.use(None)
        self.addCleanup(desktop_wlroots.use, None)
        with mock.patch.object(linux_layout, "sources", lambda said=None: iter([("a test", linux_layout.Names("us"))])):
            self.check_backend("evdev")

    def altgr_at(self, caps):
        """AltGr+à on an AZERTY keyboard, from the evdev capture through the bridge to the sender."""
        import desktop_wlroots
        import linux_layout
        import xkb_x11

        try:
            keymap = xkb_x11.Keymap.from_names("fr")
        except (RuntimeError, OSError) as exc:
            self.skipTest(f"needs libxkbcommon and the fr layout: {exc}")
        self.addCleanup(keymap.close)
        desktop_wlroots.use(None)
        self.addCleanup(desktop_wlroots.use, None)
        capture = platform_parts.load("linux", {"XDG_SESSION_TYPE": "wayland", "BEAMER_BACKEND": "evdev"}).capture
        rig = Rig()
        rig.links.caps_of[B] = caps
        assert rig.sender.set_redirecting(True)
        application = shell(rig, capture)
        with mock.patch.object(kvm_bridge_win, "capture_win", capture), mock.patch.object(sender, "OWN_PLATFORM", "linux"), \
                mock.patch.object(linux_layout, "sources", lambda said=None: iter([("a test", linux_layout.Names("fr"))])):
            hooks = capture.Hooks(lambda *args: kvm_bridge_win.WindowsApplication._on_hook_key(application, *args),
                                  lambda *_: False, lambda *_: None)
            hooks._keymap_factory = lambda said=None: (keymap, None, "a test")
            # As the bridge wires it.
            hooks.text_while(rig.sender.types_text)
            for code, down in ((100, True), (11, True), (11, False), (100, False)):
                hooks._key(1, code, down)
        return [(message["type"], message["data"].get("key", message["data"].get("text")))
                for message in rig.sent(B) if message["type"] in protocol.INPUT_TYPES]

    def test_evdev_altgr_character_goes_as_text_with_altgr_let_go_round_it(self):
        self.assertEqual(self.altgr_at(frozenset({"text"})), [
            (protocol.MSG_KEYDOWN, "alt_r"), (protocol.MSG_KEYUP, "alt_r"), (protocol.MSG_TEXT, "@"),
            (protocol.MSG_KEYDOWN, "alt_r"), (protocol.MSG_KEYUP, "alt_r")])

    def test_evdev_altgr_for_a_peer_without_text_is_the_two_keys_as_before(self):
        self.assertEqual(self.altgr_at(frozenset()), [
            (protocol.MSG_KEYDOWN, "alt_r"), (protocol.MSG_KEYDOWN, "à"), (protocol.MSG_KEYUP, "à"), (protocol.MSG_KEYUP, "alt_r")])

    def test_windows_dead_key_and_composed_text_still_reach_sender(self):
        rig = Rig()
        rig.links.caps_of[B] = frozenset({"text"})
        assert rig.sender.set_redirecting(True)
        application = shell(rig, capture_win)
        callback = kvm_bridge_win.WindowsApplication._on_hook_key
        with mock.patch.object(kvm_bridge_win, "capture_win", capture_win):
            assert callback(application, capture_win.DeadKey("^"), True, 0xDD)
            assert rig.sent(B, protocol.MSG_KEYDOWN) == []
            assert rig.sent(B, protocol.MSG_TEXT) == []
            assert callback(application, capture_win.TextInput("^x"), True, 0x58)
            assert rig.sent(B, protocol.MSG_TEXT) == [{"type": protocol.MSG_TEXT, "data": {"text": "^x"}}]
