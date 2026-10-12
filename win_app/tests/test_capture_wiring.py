"""What the shell wires into each Linux capture, over the real Hooks of every back end, none of
them started: a capture that stops while input is away must be able to say so on X11, behind the
portal and on the evdev parts alike."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kvm_bridge_win
import platform_parts

BACK_ENDS = {
    "x11": {"XDG_SESSION_TYPE": "x11"},
    "portal": {"XDG_SESSION_TYPE": "wayland", "BEAMER_BACKEND": "portal"},
    "evdev": {"XDG_SESSION_TYPE": "wayland", "BEAMER_BACKEND": "evdev"},
}


def wired(back_end):
    capture = platform_parts.load("linux", BACK_ENDS[back_end]).capture
    homes = []
    sender = SimpleNamespace(redirecting=False, types_text=lambda: True, zone_models=lambda: [],
                             set_redirecting=homes.append, input_held=None)
    stopped = []
    shell = SimpleNamespace(hooks=capture.Hooks(lambda *_: False, lambda *_: False, lambda *_: None),
                            sender=sender, _capture_stopped=stopped.append)
    kvm_bridge_win.WindowsApplication._wire_capture(shell)
    return shell, stopped, homes


class WiringTest(unittest.TestCase):
    def test_every_capture_that_can_fail_while_away_tells_the_shell(self):
        for back_end in BACK_ENDS:
            with self.subTest(back_end=back_end):
                shell, stopped, _homes = wired(back_end)
                error = RuntimeError("the capture stopped")
                shell.hooks.on_failure(error)
                self.assertEqual(stopped, [error])

    def test_the_evdev_capture_is_told_whether_the_peer_types_text_and_when_input_is_away(self):
        shell, _stopped, _homes = wired("evdev")
        self.assertTrue(shell.hooks._sends_text())
        self.assertFalse(shell.hooks._away_wanted())
        shell.sender.redirecting = True
        self.assertTrue(shell.hooks._away_wanted())

    def test_the_portal_capture_can_bring_input_home_and_says_whether_it_holds_it(self):
        shell, _stopped, homes = wired("portal")
        shell.hooks.on_home()
        self.assertEqual(homes, [False])
        self.assertEqual(shell.sender.input_held(), shell.hooks.holding)

    def test_captures_that_do_not_read_altgr_themselves_are_not_asked_about_text(self):
        for back_end in ("x11", "portal"):
            with self.subTest(back_end=back_end):
                shell, _stopped, _homes = wired(back_end)
                self.assertFalse(hasattr(shell.hooks, "text_while"))


if __name__ == "__main__":
    unittest.main()
