"""clipboard_wl with wl-copy and wl-paste faked: what is read and set, how Beamer's own copy is told
from another app's, and that a desktop without the tools, or one that refuses them, costs nothing."""

import io
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clipboard_wl as clipboard

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic"


class FakeWatcher:
    """wl-paste --watch: `changed()` is one line on its stdout; `end()` is the tool exiting."""

    def __init__(self):
        self._read, self._write = __import__("os").pipe()
        self.stdout = open(self._read, "rb", buffering=0)
        self.returncode = None
        self.terminated = False

    def changed(self):
        __import__("os").write(self._write, b"\n")

    def end(self, code=1):
        self.returncode = code
        __import__("os").close(self._write)

    def wait(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.end(-15)


class Rig(unittest.TestCase):
    def setUp(self):
        self._reset()
        self.selection = {}          # mime to bytes: what "another app" holds
        self.copies = []             # (mime, data) each wl-copy got
        self.copy_code = 0
        self.hang = False
        self.watchers = []
        self.tools = {"wl-copy": "/usr/bin/wl-copy", "wl-paste": "/usr/bin/wl-paste"}

        def run(arguments, data=None, timeout=clipboard.TIMEOUT_SECONDS):
            if self.hang:
                raise subprocess.TimeoutExpired(arguments, timeout)
            if arguments[0].endswith("wl-copy"):
                if self.copy_code == 0:
                    self.copies.append((arguments[arguments.index("--type") + 1], data))
                return self.copy_code, b""
            if "--list-types" in arguments:
                return (0, "\n".join(self.selection).encode()) if self.selection else (1, b"")
            mime = arguments[arguments.index("--type") + 1]
            return (0, self.selection[mime]) if mime in self.selection else (1, b"")

        def spawn(path):
            watcher = FakeWatcher()
            self.watchers.append(watcher)
            return watcher

        for name, value in (("_run", run), ("_spawn_watcher", spawn), ("RESTART_SECONDS", 0.01),
                            ("_tool", lambda name: self.tools.get(name))):
            patcher = mock.patch.object(clipboard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(clipboard.atexit, "register", lambda function: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reset)

    def _reset(self):
        clipboard.stop()
        thread = clipboard._watcher
        if thread is not None:
            thread.join(timeout=2)
        clipboard._stopping.clear()
        clipboard._stamp = 0
        clipboard._ours = False
        clipboard._echoes.clear()
        clipboard._watching = False
        clipboard._watcher = clipboard._process = None
        clipboard._said.clear()

    def until(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.005)

    def another_app_copies(self, **selection):
        self.selection = selection
        before = clipboard._stamp
        self.until(lambda: self.watchers)
        self.watchers[-1].changed()
        self.until(lambda: clipboard._stamp != before)


class ReadTest(Rig):
    def test_text_and_a_png_are_read_as_they_are(self):
        self.selection = {"text/plain;charset=utf-8": "héllo\r\nworld".encode(), "image/png": PNG, "text/html": b"<b>x</b>"}
        self.assertEqual(clipboard.get_contents(), ("héllo\nworld", PNG))

    def test_the_best_text_kind_on_offer_is_the_one_read(self):
        self.selection = {"STRING": b"plain", "UTF8_STRING": "précis".encode()}
        self.assertEqual(clipboard.get_contents(), ("précis", None))

    def test_another_kind_of_picture_becomes_a_png(self):
        self.selection = {"image/jpeg": b"jpeg bytes"}
        with mock.patch.object(clipboard, "_to_png", lambda data: PNG if data == b"jpeg bytes" else None):
            self.assertEqual(clipboard.get_contents(), (None, PNG))

    def test_an_empty_clipboard_and_one_too_large_read_as_nothing(self):
        self.assertEqual(clipboard.get_contents(), (None, None))
        self.selection = {"text/plain": b"x" * 10}
        with mock.patch.object(clipboard, "MAX_BYTES", 5), self.assertLogs(clipboard.LOGGER, "WARNING"):
            self.assertEqual(clipboard.get_contents(), (None, None))

    def test_an_app_that_never_hands_its_copy_over_is_waited_for_no_longer_than_the_timeout(self):
        self.hang = True
        with self.assertLogs(clipboard.LOGGER, "WARNING"):
            self.assertEqual(clipboard.get_contents(), (None, None))


class WriteTest(Rig):
    def test_text_is_set_as_utf8_with_plain_line_ends(self):
        self.assertTrue(clipboard.set_contents("héllo\r\nworld", None))
        self.assertEqual(self.copies, [("text/plain;charset=utf-8", "héllo\nworld".encode())])

    def test_a_picture_alone_is_set_and_text_wins_when_both_come(self):
        self.assertTrue(clipboard.set_contents(None, PNG))
        self.assertTrue(clipboard.set_contents("cells", PNG))
        self.assertEqual(self.copies, [("image/png", PNG), ("text/plain;charset=utf-8", b"cells")])

    def test_nothing_to_set_is_false_and_runs_nothing(self):
        self.assertFalse(clipboard.set_contents(None, None))
        self.assertEqual(self.copies, [])

    def test_a_desktop_that_refuses_wl_copy_is_false_logged_once_and_not_ours(self):
        self.copy_code = 1
        with self.assertLogs(clipboard.LOGGER, "WARNING") as logged:
            self.assertFalse(clipboard.set_contents("a", None))
            self.assertFalse(clipboard.set_contents("b", None))
        self.assertEqual(len(logged.records), 1)
        self.selection = {"text/plain": b"theirs"}
        self.assertEqual(clipboard.get_contents(), ("theirs", None))

    def test_a_wl_copy_that_hangs_is_given_up_on(self):
        self.hang = True
        with self.assertLogs(clipboard.LOGGER, "WARNING"):
            self.assertFalse(clipboard.set_contents("a", None))


class StampTest(Rig):
    def test_another_apps_copy_moves_the_stamp(self):
        first = clipboard.change_stamp()
        self.another_app_copies(**{"text/plain": b"one"})
        second = clipboard.change_stamp()
        self.another_app_copies(**{"text/plain": b"two"})
        self.assertEqual(len({first, second, clipboard.change_stamp()}), 3)

    def test_beamers_own_copy_moves_the_stamp_once_and_its_echo_does_not_move_it_again(self):
        before = clipboard.change_stamp()
        self.until(lambda: self.watchers)
        self.assertTrue(clipboard.set_contents("from a peer", None))
        after = clipboard.change_stamp()
        self.assertNotEqual(before, after)
        self.watchers[-1].changed()  # the watcher telling of that same copy
        self.until(lambda: not clipboard._echoes)
        self.assertEqual(clipboard.change_stamp(), after)

    def test_what_a_peer_set_here_is_not_read_back_until_another_app_copies(self):
        clipboard.change_stamp()
        self.until(lambda: self.watchers)
        clipboard.set_contents("from a peer", None)
        self.selection = {"text/plain": b"from a peer"}
        self.assertEqual(clipboard.get_contents(), (None, None))
        self.watchers[-1].changed()
        self.until(lambda: not clipboard._echoes)
        self.assertEqual(clipboard.get_contents(), (None, None))
        self.another_app_copies(**{"text/plain": b"mine now"})
        self.assertEqual(clipboard.get_contents(), ("mine now", None))

    def test_an_echo_that_never_came_does_not_swallow_a_later_copy(self):
        clipboard.change_stamp()
        self.until(lambda: self.watchers)
        with mock.patch.object(clipboard, "ECHO_SECONDS", -1.0):
            clipboard.set_contents("from a peer", None)
        after = clipboard.change_stamp()
        self.another_app_copies(**{"text/plain": b"later"})
        self.assertNotEqual(clipboard.change_stamp(), after)

    def test_a_watcher_that_ends_is_started_again(self):
        clipboard.change_stamp()
        self.until(lambda: self.watchers)
        with mock.patch.object(clipboard, "QUICK_EXIT_SECONDS", 0.0):
            self.watchers[0].end(1)
            self.until(lambda: len(self.watchers) == 2)
        self.assertIsNotNone(clipboard.change_stamp())

    def test_a_desktop_that_will_not_be_watched_has_no_stamp_and_is_not_tried_for_ever(self):
        with mock.patch.object(clipboard, "QUICK_EXIT_SECONDS", 60.0), self.assertLogs(clipboard.LOGGER, "WARNING"):
            clipboard.change_stamp()
            for index in range(clipboard.QUICK_EXITS):
                self.until(lambda: len(self.watchers) > index)
                self.watchers[index].end(1)
            self.until(lambda: not clipboard._watching)
        self.assertIsNone(clipboard.change_stamp())
        self.assertEqual(len(self.watchers), clipboard.QUICK_EXITS)
        # Reading and setting still go through, for what they are worth there.
        self.assertTrue(clipboard.set_contents("a", None))


class NoToolsTest(Rig):
    def test_without_wl_clipboard_nothing_is_read_set_or_watched_and_nothing_raises(self):
        self.tools = {}
        self.assertEqual(clipboard.get_contents(), (None, None))
        self.assertFalse(clipboard.set_contents("a", PNG))
        self.assertIsNone(clipboard.change_stamp())
        self.assertEqual(self.watchers, [])


class ToolTest(unittest.TestCase):
    def test_a_missing_tool_is_said_once(self):
        clipboard._said.clear()
        with mock.patch.object(clipboard.shutil, "which", lambda name: None), self.assertLogs(clipboard.LOGGER, "WARNING") as logged:
            self.assertIsNone(clipboard._tool("wl-copy"))
            self.assertIsNone(clipboard._tool("wl-copy"))
        self.assertEqual(len(logged.records), 1)
        clipboard._said.clear()

    def test_a_real_tool_is_run_to_its_end_and_a_slow_one_is_killed(self):
        self.assertEqual(clipboard._run(["/bin/sh", "-c", "printf abc"]), (0, b"abc"))
        self.assertEqual(clipboard._run(["/bin/sh", "-c", "cat >/dev/null; exit 3"], b"in"), (3, b""))
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            clipboard._run(["/bin/sh", "-c", "sleep 5"], timeout=0.1)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_picture_qt_reads_becomes_a_png_and_rubbish_does_not(self):
        try:
            from PySide6.QtCore import QBuffer, QIODevice
            from PySide6.QtGui import QImage
        except ImportError:
            self.skipTest("needs PySide6")
        image = QImage(2, 2, QImage.Format.Format_RGB32)
        image.fill(0xFF336699)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "BMP")
        png = clipboard._to_png(bytes(buffer.data()))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIsNone(clipboard._to_png(b"not a picture"))


if __name__ == "__main__":
    unittest.main()
