"""The way out through a screen edge on a desktop with no InputCapture portal: the evdev capture's
moves, the real sender's edge models and the compositor's word for where the pointer is, with no
device, no socket and no compositor."""

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capture_libinput as capture
import desktop_wlroots
import links_rig
import linux_layout
from core import protocol
from core.return_edge import Rect
from links_rig import B
from test_capture_libinput import FakeContext, FakeGuard, FakeSystem, TOUCHPAD

TURN = 0.02  # between two moves told at home


class FakeHyprland:
    name = "hyprland"

    def __init__(self, at, tells_cursor=True):
        self.at, self.tells_cursor = at, tells_cursor
        self.asked = 0
        self.moves = []

    def monitors(self):
        return [Rect(0, 0, 1920, 1080)]

    def cursor(self):
        self.asked += 1
        return self.at

    def move(self, x, y):
        self.moves.append((x, y))
        self.at = (x, y)

    def full_screen(self):
        return False


class Crossing(unittest.TestCase):
    """This machine with one display, B through its left edge."""

    def rig(self, at=(0, 500), tells_cursor=True):
        self.ipc = FakeHyprland(at, tells_cursor)
        desktop_wlroots.use(self.ipc)
        self.addCleanup(desktop_wlroots.use, None)
        with mock.patch.object(links_rig.harness, "FakeDesktop", lambda monitors, cursor: desktop_wlroots):
            self.links = links_rig.Rig()
        self.system, self.now, contexts = FakeSystem(), 1000.0, []
        self.hooks = capture.Hooks(
            lambda *key: True, lambda *event: True, self.links.sender.on_motion, system=self.system,
            context=lambda *args: contexts.append(FakeContext(*args)) or contexts[-1], guard=FakeGuard,
            keymap=lambda said=None: (None, linux_layout.Names("us"), "a test"), clock=lambda: self.now, environ={})
        # As the bridge wires it.
        self.hooks.grab_while(lambda: self.links.sender.redirecting)
        self.hooks._context = self.hooks._context_factory(self.hooks._open, self.hooks._close, True, False)
        self.addCleanup(self.hooks._end)
        self.hooks._scan()
        self.touchpad = next(device for device, path in self.hooks._paths.items() if path == TOUCHPAD)

    def push(self, times=6, dx=-20.0, dy=0.0):
        for _ in range(times):
            self.now += TURN
            self.hooks._handle(("motion", self.touchpad, dx, dy))
            self.hooks._settle(self.now)
        self.links.flush()

    def test_pushing_through_the_edge_takes_the_devices_and_the_moves_go_to_the_peer(self):
        self.rig()
        self.push()
        self.assertTrue(self.links.sender.redirecting)
        self.assertTrue(self.hooks.grabbed)
        self.links.accept_take()
        before = len(self.links.sent(B, protocol.MSG_MOUSEMOVE))
        self.hooks._handle(("motion", self.touchpad, 5.0, 3.0))
        moves = self.links.sent(B, protocol.MSG_MOUSEMOVE)[before:]
        self.assertEqual([move["data"] for move in moves], [{"dx": 5, "dy": 3}])

    def test_the_edge_lights_as_the_push_builds(self):
        self.rig()
        self.push(times=1)
        self.assertFalse(self.links.sender.redirecting)
        self.assertTrue(self.links.pressure)
        self.assertEqual(self.links.pressure[-1][0], "left")

    def test_moving_about_the_middle_of_the_screen_goes_nowhere(self):
        self.rig(at=(900, 500))
        self.push(times=30)
        self.assertFalse(self.links.sender.redirecting)
        self.assertFalse(self.hooks.grabbed)

    def test_pushing_away_from_the_edge_goes_nowhere(self):
        self.rig()
        self.push(times=30, dx=20.0)
        self.assertFalse(self.links.sender.redirecting)

    def test_the_compositor_is_asked_where_the_pointer_is_once_for_each_move_told_not_for_each_event(self):
        self.rig(at=(900, 500))
        self.now += TURN
        for _ in range(200):  # one turn of the loop on a fast mouse
            self.hooks._handle(("motion", self.touchpad, -1.0, 0.0))
        self.assertLessEqual(self.ipc.asked, 1)

    def test_where_the_compositor_does_not_say_where_the_pointer_is_no_edge_is_crossed(self):
        # sway: the shortcut alone sends input away.
        self.rig(tells_cursor=False)
        self.push(times=30)
        self.assertFalse(self.links.sender.redirecting)
        self.assertFalse(self.hooks.grabbed)

    def test_the_peer_pushing_back_lets_go_of_the_devices_and_lands_the_pointer_on_this_edge(self):
        self.rig()
        self.push()
        self.links.accept_take()
        self.assertTrue(self.hooks.grabbed)
        self.links.inbound(B, protocol.switch_v6(self.links.sender._owner.route, links_rig.HERE, edge="left", offset=0.5))
        self.assertFalse(self.links.sender.redirecting)
        self.hooks._settle(self.now + TURN)
        self.assertFalse(self.hooks.grabbed)
        self.assertEqual(self.system.grabs, set())
        # Hyprland is told where, and the arrival is drawn there.
        self.assertEqual(self.ipc.moves, [(0, 540)])
        self.assertEqual(self.links.arrivals, [("left", 0, 540)])

    def test_home_again_the_same_edge_wants_a_new_push(self):
        self.rig()
        self.push()
        self.links.accept_take()
        self.links.inbound(B, protocol.switch_v6(self.links.sender._owner.route, links_rig.HERE, edge="left", offset=0.5))
        self.hooks._settle(self.now + TURN)
        self.push(times=1)
        self.assertFalse(self.links.sender.redirecting)


if __name__ == "__main__":
    unittest.main()
