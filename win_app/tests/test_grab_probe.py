"""tools/grab_probe.py's count for each device, over a made-up libinput: which device made what, and
never which key."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

spec = importlib.util.spec_from_file_location("grab_probe", Path(__file__).resolve().parents[2] / "tools" / "grab_probe.py")
grab_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grab_probe)

KEYBOARD, TOUCHPAD, OWN = "/dev/input/event3", "/dev/input/event11", "/dev/input/event20"
KEY_A, BTN_LEFT = 30, 0x110


class FakeSystem:
    names = {KEYBOARD: "AT Translated Set 2 keyboard", TOUCHPAD: "ASUE1234:00 04F3:3210 Touchpad", OWN: "Beamer virtual keyboard"}

    def paths(self):
        return [TOUCHPAD, KEYBOARD, OWN]

    def name(self, path):
        return self.names[path]

    read_only = False

    def open(self, path, flags):
        if self.read_only and flags & os.O_ACCMODE != os.O_RDONLY:
            raise PermissionError(13, "read only")
        self.flags = flags
        return 0

    def close(self, fd):
        pass


class FakeContext:
    """libinput whose events come all at once, at the first look."""

    def __init__(self, script, opener):
        self._script, self.opener = script, opener
        self.devices = {}
        self._read, self._write = os.pipe()
        os.write(self._write, b"x")
        self.closed = False

    def fileno(self):
        return self._read

    def add(self, path):
        try:
            self.opener(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            return None
        self.devices[len(self.devices) + 1] = path
        return len(self.devices)

    def events(self):
        by_path = {path: device for device, path in self.devices.items()}
        found, self._script = [(kind, by_path[path], *rest) for kind, path, *rest in self._script], []
        return found

    def close(self):
        self.closed = True
        os.close(self._read)
        os.close(self._write)


class ByDeviceTest(unittest.TestCase):
    def probe(self, script, seconds=0.3, read_only=False):
        self.system = FakeSystem()
        self.system.read_only = read_only
        made = []

        def context(opener, closer, tap, natural):
            made.append(FakeContext(script, opener))
            return made[-1]

        ticks = iter(range(1000))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = grab_probe.by_device(seconds, system=self.system, context=context, clock=lambda: next(ticks) * 0.1)
        self.context = made[-1]
        return status, out.getvalue()

    def test_each_device_is_named_with_what_it_made_and_a_silent_one_says_so(self):
        status, said = self.probe([
            ("motion", TOUCHPAD, 1.5, 0.0), ("motion", TOUCHPAD, 0.2, 0.1), ("scroll", TOUCHPAD, 3.0, 0.0),
            ("button", TOUCHPAD, BTN_LEFT, True), ("button", TOUCHPAD, BTN_LEFT, False), ("other", TOUCHPAD, 806),
        ])
        self.assertEqual(status, 0)
        lines = said.splitlines()
        keyboard, touchpad = lines.index("event3   AT Translated Set 2 keyboard"), lines.index("event11  ASUE1234:00 04F3:3210 Touchpad")
        self.assertLess(keyboard, touchpad)
        self.assertEqual(lines[keyboard + 1].strip(), "nothing")
        self.assertEqual([line.split(None, 1) for line in lines[touchpad + 1:]],
                         [["1", "button down"], ["1", "button up"], ["1", "gesture hold begin (not sent on)"],
                          ["2", "motion"], ["1", "scroll"]])
        self.assertTrue(self.context.closed)

    def test_beamers_own_devices_are_not_read(self):
        self.probe([])
        self.assertEqual(sorted(self.context.devices.values()), sorted([KEYBOARD, TOUCHPAD]))

    def test_devices_this_user_may_only_read_are_read_as_the_capture_reads_them(self):
        status, said = self.probe([("motion", TOUCHPAD, 1.0, 0.0)], read_only=True)
        self.assertEqual(status, 0)
        self.assertIn("1  motion", said)
        self.assertEqual(self.system.flags & os.O_ACCMODE, os.O_RDONLY)

    def test_a_key_is_counted_and_never_told(self):
        _status, said = self.probe([("key", KEYBOARD, KEY_A, True), ("key", KEYBOARD, KEY_A, False)])
        self.assertIn("1  key down", said)
        self.assertIn("1  key up", said)
        self.assertNotIn(str(KEY_A), said)

    def test_an_event_with_no_name_is_counted_by_its_number(self):
        self.assertEqual(grab_probe.kind_of(("other", 1, 999)), "libinput event 999 (not sent on)")


class FakeHooks:
    """The capture as `listen` drives it: at start it plays what a finger drawing circles on the
    touchpad makes, told only if moves at home are asked for."""

    made = []

    def __init__(self, on_key, on_mouse, on_motion, environ=None):
        self.on_motion, self.home_moves, self.grabbed, self._paths = on_motion, None, False, {1: TOUCHPAD}
        FakeHooks.made.append(self)

    def grab_while(self, predicate):
        pass

    def start(self):
        for _ in range(3):
            if self.home_moves is not None and self.home_moves():
                self.on_motion(2, 1)

    def stop(self):
        pass


class ListenTest(unittest.TestCase):
    def test_listening_counts_the_moves_made_at_home_with_no_compositor_to_ask(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = grab_probe.run(0.0, grab=False, hooks_class=FakeHooks)
        self.assertEqual(status, 0)
        self.assertIn("     3  motion", out.getvalue())
        self.assertNotIn("No event arrived.", out.getvalue())


if __name__ == "__main__":
    unittest.main()
