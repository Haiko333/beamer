"""grab_guard: the process that ends a Beamer stuck with the keyboard. The watching is tested in
this process with a pipe and a kill that only notes; then once for real, on a child that plays a
hung Beamer, never on the test run itself."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grab_guard


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.read, self.write = os.pipe()
        self.addCleanup(os.close, self.read)
        self.killed = []
        self.result = []

    def watch(self, limit=0.15):
        thread = threading.Thread(
            target=lambda: self.result.append(grab_guard.watch(limit, self.read, os.getppid(), lambda pid, sig: self.killed.append((pid, sig)))),
            daemon=True)
        thread.start()
        return thread

    def close(self):
        os.close(self.write)

    def test_silence_before_any_grab_is_nothing(self):
        thread = self.watch()
        time.sleep(0.4)
        self.assertTrue(thread.is_alive())
        self.close()
        thread.join(2)
        self.assertEqual((self.result, self.killed), (["gone"], []))

    def test_silence_while_grabbed_ends_the_parent(self):
        thread = self.watch()
        os.write(self.write, grab_guard.GRABBED)
        thread.join(2)
        self.close()
        self.assertEqual((self.result, self.killed), (["killed"], [(os.getppid(), signal.SIGKILL)]))

    def test_beats_while_grabbed_keep_it_quiet_and_letting_go_disarms_it(self):
        thread = self.watch()
        for _ in range(8):
            os.write(self.write, grab_guard.GRABBED)
            time.sleep(0.05)
        self.assertEqual(self.killed, [])
        os.write(self.write, grab_guard.LET_GO)
        time.sleep(0.4)
        self.assertTrue(thread.is_alive())
        self.close()
        thread.join(2)
        self.assertEqual((self.result, self.killed), (["gone"], []))

    def test_a_parent_that_is_already_gone_is_not_killed_under_its_old_number(self):
        # Armed and silent, but the process it was started by is no longer its parent: that pid
        # may be another program's by now.
        os.write(self.write, grab_guard.GRABBED)
        not_ours = os.getppid() + 1
        result = grab_guard.watch(0.05, self.read, parent=not_ours, kill=lambda pid, sig: self.killed.append(pid))
        self.close()
        self.assertEqual((result, self.killed), ("killed", []))


@unittest.skipIf(sys.platform == "win32", "the guard is the Linux capture's")
class ProcessTest(unittest.TestCase):
    """For real: a stand-in for Beamer starts the guard, says it has the keyboard, and hangs."""

    HUNG = """
import subprocess, sys, time
guard = subprocess.Popen([sys.executable, sys.argv[1], "0.3"], stdin=subprocess.PIPE)
guard.stdin.write(b"%s"); guard.stdin.flush()
print("grabbed", flush=True)
time.sleep(30)
"""

    def run_stand_in(self, word):
        return subprocess.Popen([sys.executable, "-c", self.HUNG % word.decode(), grab_guard.__file__],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def test_a_hung_process_holding_the_keyboard_is_killed(self):
        stand_in = self.run_stand_in(grab_guard.GRABBED)
        self.addCleanup(lambda: stand_in.poll() is None and stand_in.kill())
        self.assertEqual(stand_in.stdout.readline().strip(), b"grabbed")
        self.assertEqual(stand_in.wait(timeout=5), -signal.SIGKILL)
        stand_in.stdout.close()

    def test_the_captures_own_guard_starts_is_told_and_ends_with_its_pipe(self):
        import capture_libinput

        # Never told the devices are grabbed: this process is its parent.
        guard = capture_libinput.Guard(30.0)
        self.assertTrue(guard.alive())
        guard.beat(False)
        guard.beat(False)
        guard.close()
        self.assertFalse(guard.alive())
        with self.assertRaises(OSError):
            guard.beat(True)

    def test_a_hung_process_that_holds_nothing_is_left_alone(self):
        stand_in = self.run_stand_in(grab_guard.LET_GO)
        self.assertEqual(stand_in.stdout.readline().strip(), b"grabbed")
        time.sleep(1.0)
        self.assertIsNone(stand_in.poll())
        stand_in.kill()
        stand_in.wait(timeout=5)
        stand_in.stdout.close()


if __name__ == "__main__":
    unittest.main()
