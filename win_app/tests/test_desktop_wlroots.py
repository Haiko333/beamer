"""desktop_wlroots against fake compositor sockets: what Hyprland and sway say is read as logical
rectangles, and a compositor that says nothing, or stops answering, leaves Beamer's own notes."""

import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import desktop_wlroots as desktop
from core.return_edge import Rect

# One 2880x1800 panel at 4/3, as a laptop reports it, and a 1080p display on its side to the right.
HYPRLAND_MONITORS = [
    {"name": "eDP-1", "width": 2880, "height": 1800, "x": 0, "y": 0, "scale": 1.3333334, "transform": 0, "disabled": False},
    {"name": "DP-1", "width": 1920, "height": 1080, "x": 2160, "y": 0, "scale": 1.0, "transform": 1, "disabled": False},
    {"name": "DP-2", "width": 1920, "height": 1080, "x": 9000, "y": 0, "scale": 1.0, "transform": 0, "disabled": True},
]
LAPTOP, TURNED = Rect(0, 0, 2160, 1350), Rect(2160, 0, 1080, 1920)


class FakeIpc:
    name = "fake"

    def __init__(self, tells_cursor=True):
        self.tells_cursor = tells_cursor
        self.displays = [LAPTOP, TURNED]
        self.pointer = (100, 200)
        self.moves = []
        self.window = None
        self.broken = set()
        self.asked = []

    def _ask(self, what):
        self.asked.append(what)
        if what in self.broken:
            raise OSError(f"no {what}")

    def monitors(self):
        self._ask("monitors")
        return list(self.displays)

    def cursor(self):
        self._ask("cursor")
        return self.pointer

    def move(self, x, y):
        self._ask("move")
        self.moves.append((x, y))

    def full_screen(self):
        self._ask("full_screen")
        return self.window


class ReadingTest(unittest.TestCase):
    def test_hyprland_displays_are_logical_turned_ones_swap_and_disabled_ones_are_left_out(self):
        self.assertEqual(desktop.hyprland_monitors(HYPRLAND_MONITORS), [LAPTOP, TURNED])

    def test_hyprland_full_screen_is_two_or_three_not_maximised(self):
        for state, expected in ((0, None), (1, None), (2, "mpv"), (3, "mpv"), (True, "mpv"), (False, None)):
            with self.subTest(state=state):
                self.assertEqual(desktop.hyprland_full_screen({"fullscreen": state, "class": "mpv"}), expected)
        # No window has the focus on an empty workspace.
        self.assertIsNone(desktop.hyprland_full_screen({}))
        self.assertEqual(desktop.hyprland_full_screen({"fullscreen": 2, "class": ""}), "An app")

    def test_sway_displays_are_its_active_outputs(self):
        outputs = [
            {"name": "eDP-1", "active": True, "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
            {"name": "HDMI-A-1", "active": False, "rect": {"x": 0, "y": 0, "width": 0, "height": 0}},
            {"name": "DP-1", "active": True, "rect": {"x": 1920, "y": -200, "width": 2560, "height": 1440}},
        ]
        self.assertEqual(desktop.sway_monitors(outputs), [Rect(0, 0, 1920, 1080), Rect(1920, -200, 2560, 1440)])

    def test_sway_full_screen_is_the_focused_container_wherever_it_sits(self):
        def tree(focused):
            return {"nodes": [{"nodes": [{"nodes": [{"app_id": "foot", "focused": False, "fullscreen_mode": 1}],
                                           "floating_nodes": [focused]}]}]}

        self.assertEqual(sway_name(tree({"app_id": "mpv", "focused": True, "fullscreen_mode": 1})), "mpv")
        self.assertIsNone(sway_name(tree({"app_id": "mpv", "focused": True, "fullscreen_mode": 0})))
        xwayland = {"app_id": None, "window_properties": {"class": "Steam"}, "focused": True, "fullscreen_mode": 1}
        self.assertEqual(sway_name(tree(xwayland)), "Steam")
        self.assertIsNone(sway_name({"nodes": []}))


def sway_name(tree):
    return desktop.sway_full_screen(tree)


class FindTest(unittest.TestCase):
    def test_the_socket_is_named_by_the_compositors_own_variables(self):
        hyprland = desktop.find_ipc({"HYPRLAND_INSTANCE_SIGNATURE": "abc_1", "XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertEqual((hyprland.name, hyprland.path), ("hyprland", "/run/user/1000/hypr/abc_1/.socket.sock"))
        sway = desktop.find_ipc({"SWAYSOCK": "/run/user/1000/sway-ipc.1000.1.sock"})
        self.assertEqual((sway.name, sway.path, sway.tells_cursor), ("sway", "/run/user/1000/sway-ipc.1000.1.sock", False))
        self.assertIsNone(desktop.find_ipc({"XDG_CURRENT_DESKTOP": "GNOME"}))


class DesktopTest(unittest.TestCase):
    def setUp(self):
        self.ipc = FakeIpc()
        desktop.use(self.ipc)
        desktop.set_mover(None)
        self.addCleanup(desktop.use, None)
        self.addCleanup(desktop.set_mover, None)

    def test_the_compositor_answers_for_displays_pointer_and_full_screen(self):
        self.ipc.window = "mpv"
        self.assertEqual(desktop.monitors(), [LAPTOP, TURNED])
        self.assertEqual(desktop.display_map(), {"0": LAPTOP, "1": TURNED})
        self.assertEqual(desktop.cursor_position(), (100, 200))
        self.assertEqual(desktop.full_screen_app(), "mpv")
        self.assertEqual(desktop.compositor(), "fake")
        self.assertTrue(desktop.tells_cursor())
        self.assertFalse(desktop.button_down("left"))

    def test_the_displays_are_asked_once_a_second_not_on_every_move(self):
        for _ in range(50):
            desktop.monitors()
        self.assertEqual(self.ipc.asked.count("monitors"), 1)
        with mock.patch.object(desktop.time, "monotonic", return_value=desktop.time.monotonic() + 2):
            self.ipc.displays = [LAPTOP]
            self.assertEqual(desktop.monitors(), [LAPTOP])

    def test_a_placement_goes_to_the_compositor_held_on_a_display(self):
        desktop.set_cursor_position(500, 600)
        desktop.set_cursor_position(99999, -5)
        self.assertEqual(self.ipc.moves, [(500, 600), (TURNED.right, 0)])

    def test_a_compositor_that_stops_answering_never_raises_and_is_reported_once(self):
        desktop.cursor_position()
        desktop.monitors()
        self.ipc.broken = {"monitors", "cursor", "move", "full_screen"}
        with mock.patch.object(desktop.time, "monotonic", return_value=desktop.time.monotonic() + 2), \
                self.assertLogs(desktop.LOGGER, "WARNING") as logged:
            self.assertEqual(desktop.cursor_position(), (100, 200))  # the last it said
            for _ in range(3):
                self.assertEqual(desktop.monitors(), [LAPTOP, TURNED])  # the last it named
                desktop.set_cursor_position(10, 20)
                self.assertEqual(desktop.cursor_position(), (10, 20))
                self.assertIsNone(desktop.full_screen_app())
        self.assertEqual(len(logged.records), 4)

    def test_without_a_pointer_answer_the_notes_are_the_position(self):
        self.ipc = FakeIpc(tells_cursor=False)
        desktop.use(self.ipc)
        self.assertFalse(desktop.tells_cursor())
        # Nothing noted yet: the middle of the first display.
        self.assertEqual(desktop.cursor_position(), (1080, 675))
        desktop.set_cursor_position(300, 400)
        desktop.note_motion(10.4, -5.4)
        desktop.note_motion(0.4, 0.2)
        self.assertEqual(desktop.cursor_position(), (311, 395))
        self.assertNotIn("cursor", self.ipc.asked)

    def test_noted_moves_stop_at_the_edge_of_the_displays(self):
        self.ipc = FakeIpc(tells_cursor=False)
        desktop.use(self.ipc)
        desktop.set_cursor_position(5, 5)
        desktop.note_motion(-500, -500)
        self.assertEqual(desktop.cursor_position(), (0, 0))
        desktop.note_motion(3, 2)
        self.assertEqual(desktop.cursor_position(), (3, 2))

    def test_no_compositor_socket_means_qt_displays_the_notes_and_the_injectors_mover(self):
        desktop.use(None)
        moved = []
        desktop.set_mover(lambda x, y: moved.append((x, y)))
        self.assertEqual(desktop.compositor(), "")
        self.assertFalse(desktop.tells_cursor())
        self.assertEqual(desktop.monitors(), [Rect(0, 0, 1920, 1080)])
        with mock.patch.object(desktop, "_screens", [Rect(0, 0, 2560, 1440)]):
            self.assertEqual(desktop.monitors(), [Rect(0, 0, 2560, 1440)])
            desktop.set_cursor_position(2000, 1000)
            self.assertEqual(desktop.cursor_position(), (2000, 1000))
        self.assertEqual(moved, [(2000, 1000)])
        self.assertIsNone(desktop.full_screen_app())

    def test_a_mover_that_fails_is_logged_not_raised(self):
        desktop.use(None)

        def broken(x, y):
            raise OSError("device gone")

        desktop.set_mover(broken)
        with self.assertLogs(desktop.LOGGER, "ERROR"):
            desktop.set_cursor_position(1, 1)


class SocketTest(unittest.TestCase):
    """The two wire formats, against a real unix socket."""

    def serve(self, answer):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "ipc.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(4)
        self.addCleanup(server.close)

        def run():
            while True:
                try:
                    link, _ = server.accept()
                except OSError:
                    return
                with link:
                    answer(link)

        threading.Thread(target=run, daemon=True).start()
        return path

    def test_hyprland_sends_the_command_and_reads_until_the_socket_closes(self):
        seen = []

        def answer(link):
            command = link.recv(4096).decode()
            seen.append(command)
            replies = {"j/monitors": json.dumps(HYPRLAND_MONITORS), "j/cursorpos": '{"x": 12, "y": 34}',
                       "j/activewindow": '{"fullscreen": 2, "class": "mpv"}'}
            link.sendall(replies.get(command, "ok").encode())

        ipc = desktop.Hyprland(self.serve(answer))
        self.assertEqual(ipc.monitors(), [LAPTOP, TURNED])
        self.assertEqual(ipc.cursor(), (12, 34))
        self.assertEqual(ipc.full_screen(), "mpv")
        ipc.move(640, 480)
        self.assertEqual(seen[-1], "dispatch hl.dsp.cursor.move({x=640, y=480})")

    def test_an_older_hyprland_is_moved_with_the_old_words_from_then_on(self):
        seen = []

        def answer(link):
            command = link.recv(4096).decode()
            seen.append(command)
            link.sendall(b"ok" if command.startswith("dispatch movecursor") else b"Invalid dispatcher")

        ipc = desktop.Hyprland(self.serve(answer))
        ipc.move(1, 2)
        ipc.move(3, 4)
        self.assertEqual(seen, ["dispatch hl.dsp.cursor.move({x=1, y=2})", "dispatch movecursor 1 2", "dispatch movecursor 3 4"])

    def test_hyprland_refusing_a_move_is_an_error(self):
        ipc = desktop.Hyprland(self.serve(lambda link: (link.recv(4096), link.sendall(b"Invalid dispatcher"))))
        with self.assertRaises(OSError):
            ipc.move(1, 2)

    def test_sway_frames_each_message_and_reads_one_framed_answer(self):
        seen = []

        def answer(link):
            header = link.recv(14)
            length, kind = struct.unpack("<II", header[6:])
            payload = link.recv(length).decode() if length else ""
            seen.append((header[:6], kind, payload))
            body = json.dumps({
                desktop.Sway.GET_OUTPUTS: [{"active": True, "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}}],
                desktop.Sway.RUN_COMMAND: [{"success": payload.startswith("seat")}],
                desktop.Sway.GET_TREE: {"nodes": [{"focused": True, "fullscreen_mode": 1, "app_id": "mpv"}]},
            }[kind]).encode()
            link.sendall(b"i3-ipc" + struct.pack("<II", len(body), kind) + body)

        ipc = desktop.Sway(self.serve(answer))
        self.assertEqual(ipc.monitors(), [Rect(0, 0, 1920, 1080)])
        self.assertEqual(ipc.full_screen(), "mpv")
        ipc.move(640, 480)
        self.assertEqual(seen[-1], (b"i3-ipc", desktop.Sway.RUN_COMMAND, "seat - cursor set 640 480"))
        with self.assertRaises(OSError):
            ipc.cursor()

    def test_a_socket_that_is_not_there_is_an_error_for_the_caller_to_absorb(self):
        with self.assertRaises(OSError):
            desktop.Hyprland("/nonexistent/beamer/.socket.sock").monitors()
        with self.assertRaises(OSError):
            desktop.Sway("/nonexistent/beamer/sway.sock").monitors()


if __name__ == "__main__":
    unittest.main()
