"""libinput.py's numbers against the installed header, and its functions against the installed
library: the binding is ctypes, so nothing else would notice either drifting. No device is opened."""

import ctypes
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import libinput

HEADER = Path("/usr/include/libinput.h")


def enum_values(text):
    """Every `NAME = number` and bare `NAME,` of the header's enums, numbered as C numbers them."""
    values = {}
    for body in re.findall(r"enum\s+\w+\s*\{(.*?)\}", re.sub(r"/\*.*?\*/", "", text, flags=re.S), flags=re.S):
        number = -1
        for item in body.split(","):
            match = re.fullmatch(r"\s*(\w+)\s*(?:=\s*(-?\d+|1\s*<<\s*\d+))?\s*", item)
            if match is None:
                continue
            if match.group(2) is not None:
                number = eval(match.group(2))  # only digits and a shift pass the pattern
            else:
                number += 1
            values[match.group(1)] = number
    return values


@unittest.skipUnless(HEADER.exists(), "needs libinput's header")
class HeaderTest(unittest.TestCase):
    def test_the_event_types_and_axes_are_the_headers(self):
        values = enum_values(HEADER.read_text())
        for ours, theirs in (
            ("EVENT_DEVICE_ADDED", "LIBINPUT_EVENT_DEVICE_ADDED"), ("EVENT_DEVICE_REMOVED", "LIBINPUT_EVENT_DEVICE_REMOVED"),
            ("EVENT_KEYBOARD_KEY", "LIBINPUT_EVENT_KEYBOARD_KEY"), ("EVENT_POINTER_MOTION", "LIBINPUT_EVENT_POINTER_MOTION"),
            ("EVENT_POINTER_BUTTON", "LIBINPUT_EVENT_POINTER_BUTTON"), ("EVENT_SCROLL_WHEEL", "LIBINPUT_EVENT_POINTER_SCROLL_WHEEL"),
            ("EVENT_SCROLL_FINGER", "LIBINPUT_EVENT_POINTER_SCROLL_FINGER"),
            ("EVENT_SCROLL_CONTINUOUS", "LIBINPUT_EVENT_POINTER_SCROLL_CONTINUOUS"),
            ("AXIS_VERTICAL", "LIBINPUT_POINTER_AXIS_SCROLL_VERTICAL"), ("AXIS_HORIZONTAL", "LIBINPUT_POINTER_AXIS_SCROLL_HORIZONTAL"),
        ):
            with self.subTest(name=theirs):
                self.assertEqual(getattr(libinput, ours), values[theirs])
        self.assertEqual((values["LIBINPUT_KEY_STATE_PRESSED"], values["LIBINPUT_BUTTON_STATE_PRESSED"]), (1, 1))

    def test_the_events_not_sent_on_are_named_as_the_header_names_them(self):
        values = enum_values(HEADER.read_text())
        theirs = {number: name for name, number in values.items() if name.startswith("LIBINPUT_EVENT_") and number >= 300}
        for number, ours in libinput.EVENT_NAMES.items():
            with self.subTest(number=number):
                self.assertIn(number, theirs)
                words = ours.replace(" (older scroll)", "").upper().replace(" ", "_")
                self.assertEqual("LIBINPUT_EVENT_" + words, theirs[number])


class LibraryTest(unittest.TestCase):
    def test_the_library_is_called_without_letting_go_of_the_interpreter(self):
        # Each call through CDLL hands the interpreter to whichever thread wants it, some nine
        # times an event: with the window or the link busy, a mouse at a thousand reports a second
        # outruns the loop and libinput says it lags. None of these calls waits for anything.
        try:
            lib = libinput._load()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        self.assertIsInstance(lib, ctypes.PyDLL)

    def test_every_function_bound_is_in_the_library_and_a_device_is_opened_only_through_the_caller(self):
        try:
            libinput._load()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        asked = []

        def refuse(path, flags):
            asked.append(path)
            raise PermissionError(13, "the test opens no device")

        context = libinput.Context(refuse)
        self.addCleanup(context.close)
        self.assertGreater(context.fileno(), 0)
        self.assertEqual(context.events(), [])
        # What it is asked to read it opens through the caller alone, who here refuses.
        self.assertIsNone(context.add("/dev/null"))
        self.assertEqual(asked, ["/dev/null"])


if __name__ == "__main__":
    unittest.main()
