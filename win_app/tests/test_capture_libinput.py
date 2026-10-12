"""capture_libinput with the kernel, libinput and the guard faked: which devices are read, when
they are grabbed and let go, each of the ways home, and what a key, a button, a move and a scroll
become for the bridge. No device of this machine is opened."""

import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import capture_libinput as capture
import capture_win
import desktop_wlroots
import linux_layout

KEYBOARD, MOUSE, TOUCHPAD, OWN = "/dev/input/event3", "/dev/input/event7", "/dev/input/event9", "/dev/input/event20"
SHIFT, SHIFT_R, CTRL_R, ESC, KEY_A, KEY_Q, CAPS = 42, 54, 97, 1, 30, 16, 58
BTN_LEFT, BTN_SIDE = 0x110, 0x113


class FakeSystem:
    def __init__(self):
        self.names = {KEYBOARD: "AT Translated Set 2 keyboard", MOUSE: "Logitech USB Receiver Mouse",
                      TOUCHPAD: "ASUF1209:00 2808:0219 Touchpad", OWN: "Beamer virtual keyboard"}
        self.denied = set()
        self.read_only = set()
        self.busy = set()
        self.broken = set()
        self.opened = {}       # fd: (path, flags)
        self.grabs = set()     # fds grabbed now
        self.grab_log = []
        self.down = {}         # path: codes the kernel has as held
        self.led = {}          # path: LEDs lit
        self.log = []          # ("release", path, code) and ("grab", path, on), in order
        self.deaf = set()      # paths whose kernel keeps a key as held whatever is written
        self._next = 100

    def paths(self):
        return sorted(self.names, key=lambda path: int(path.rsplit("event", 1)[1]))

    def name(self, path):
        return self.names.get(path, "")

    def readable(self, path):
        return path not in self.denied

    def open(self, path, flags):
        if path in self.denied or (path in self.read_only and flags & os.O_ACCMODE != os.O_RDONLY):
            raise PermissionError(13, "Permission denied")
        self._next += 1
        self.opened[self._next] = (path, flags)
        return self._next

    def close(self, fd):
        self.grabs.discard(fd)
        del self.opened[fd]

    def path_of(self, fd):
        return self.opened[fd][0]

    def grab(self, fd, on):
        if on and self.path_of(fd) in self.busy:
            raise OSError(16, "Device or resource busy")
        if on and self.path_of(fd) in self.broken:
            raise OSError(19, "No such device")
        (self.grabs.add if on else self.grabs.discard)(fd)
        self.grab_log.append((self.path_of(fd), on))
        self.log.append(("grab", self.path_of(fd), on))

    def keys_down(self, fd):
        return set(self.down.get(self.path_of(fd), ()))

    def leds(self, fd):
        return set(self.led.get(self.path_of(fd), ()))

    def release(self, fd, code):
        """As the kernel takes it: the key is up from then on, for whoever reads the device."""
        if self.opened[fd][1] & os.O_ACCMODE == os.O_RDONLY:
            raise OSError(9, "Bad file descriptor")
        path = self.path_of(fd)
        self.log.append(("release", path, code))
        if path not in self.deaf:
            self.down[path] = set(self.down.get(path, ())) - {code}


class FakeContext:
    """libinput: a device is whatever opens and is not on `unknown`; events are queued by the test."""

    def __init__(self, opener, closer, tap, natural):
        self.opener, self.closer, self.tap, self.natural = opener, closer, tap, natural
        self.unknown = set()
        self.devices = {}      # device: (path, fd)
        self.queue = []
        self._read, self._write = os.pipe()
        self.closed = False
        self._next = 0

    def fileno(self):
        return self._read

    def add(self, path):
        if path in self.unknown:
            return None
        try:
            fd = self.opener(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return None
        self._next += 1
        self.devices[self._next] = (path, fd)
        return self._next

    def remove(self, device):
        _path, fd = self.devices.pop(device)
        self.closer(fd)

    def feed(self, *events):
        self.queue.extend(events)
        os.write(self._write, b"x")

    def events(self):
        try:
            os.set_blocking(self._read, False)
            os.read(self._read, 4096)
        except OSError:
            pass
        found, self.queue = self.queue, []
        return found

    def close(self):
        for device in list(self.devices):
            self.remove(device)
        self.closed = True
        os.close(self._read)
        os.close(self._write)


class FakeGuard:
    def __init__(self):
        self.beats = []
        self.dead = False
        self.closed = False

    def alive(self):
        return not self.dead

    def beat(self, grabbed):
        if self.dead:
            raise BrokenPipeError(32, "Broken pipe")
        self.beats.append(grabbed)

    def close(self):
        self.closed = True


class FakeKeymap:
    """US, with the A key typing "q" in group 1, enough to tell a layout and a group are used."""

    closed = False

    def keysym(self, keycode, mods, group):
        code = keycode - 8
        if code == KEY_A:
            letter = "q" if group == 1 else "a"
            return ord(letter.upper() if bool(mods & 1) != bool(mods & 2) else letter)
        return 0

    def text(self, keysym):
        return chr(keysym) if keysym else None

    def close(self):
        self.closed = True


class TellsCursor:
    """A compositor that says where the pointer is, as Hyprland does."""

    name = "hyprland"
    tells_cursor = True


class Rig(unittest.TestCase):
    environ = {}

    def setUp(self):
        self.system = FakeSystem()
        self.contexts, self.guards = [], []
        self.keys, self.mouse, self.moves, self.failures, self.rows = [], [], [], [], []
        self.now = 1000.0
        self.away = False
        self.keymap = FakeKeymap()
        self.guard_error = None

        def context(*args):
            self.contexts.append(FakeContext(*args))
            return self.contexts[-1]

        def guard():
            if self.guard_error is not None:
                raise self.guard_error
            self.guards.append(FakeGuard())
            return self.guards[-1]

        self.hooks = capture.Hooks(
            lambda *key: self.keys.append(key) or True, lambda *event: self.mouse.append(event) or True,
            lambda dx, dy: self.moves.append((dx, dy)), system=self.system, context=context, guard=guard,
            keymap=lambda said=None: (self.keymap, linux_layout.Names("us"), "a test"), clock=lambda: self.now,
            environ=self.environ)
        self.hooks.grab_while(lambda: self.away)
        self.hooks.on_failure = self.failures.append
        self.hooks.on_permission = lambda *row: self.rows.append(row)
        desktop_wlroots.use(None)
        self.addCleanup(desktop_wlroots.use, None)
        patcher = mock.patch.object(linux_layout, "sources", lambda said=None: iter([("a test", linux_layout.Names("us"))]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self):
        """The thread's start, without the thread: the context made and the devices found."""
        self.hooks._context = self.hooks._context_factory(self.hooks._open, self.hooks._close, True, False)
        self.context = self.contexts[-1]
        self.addCleanup(self.hooks._end)
        return self.hooks._scan()

    def device(self, path):
        return next(device for device, known in self.hooks._paths.items() if known == path)

    def feed(self, *events):
        for event in events:
            self.hooks._handle(event)

    def turn(self, seconds=0.05):
        self.now += seconds
        self.hooks._settle(self.now)

    def grabbed_paths(self):
        return sorted(self.system.path_of(fd) for fd in self.system.grabs)

    def sentences(self):
        return [failure.sentence for failure in self.failures]


class DevicesTest(Rig):
    def test_every_keyboard_and_pointer_is_read_and_beamers_own_devices_are_not(self):
        self.assertEqual(self.open(), 0)
        self.assertEqual(sorted(self.hooks._paths.values()), [KEYBOARD, MOUSE, TOUCHPAD])
        self.assertNotIn(OWN, [path for path, _flags in self.system.opened.values()])

    def test_what_libinput_will_not_have_is_not_asked_again_and_a_denied_one_is(self):
        self.hooks._context = self.hooks._context_factory(self.hooks._open, self.hooks._close, True, False)
        self.context = self.contexts[-1]
        self.addCleanup(self.hooks._end)
        self.context.unknown = {MOUSE}       # a lid switch, say
        self.system.denied = {TOUCHPAD}
        self.assertEqual(self.hooks._scan(), 1)
        self.assertEqual(sorted(self.hooks._paths.values()), [KEYBOARD])
        self.context.unknown = set()
        self.system.denied = set()            # the udev rule is installed
        self.assertEqual(self.hooks._scan(), 0)
        self.assertEqual(sorted(self.hooks._paths.values()), [KEYBOARD, TOUCHPAD])

    def test_a_device_plugged_in_is_read_and_one_taken_away_is_forgotten(self):
        self.open()
        self.system.names["/dev/input/event30"] = "USB Keyboard"
        del self.system.names[MOUSE]
        self.hooks._scan()
        self.assertEqual(set(self.hooks._paths.values()), {KEYBOARD, TOUCHPAD, "/dev/input/event30"})
        self.assertNotIn(MOUSE, [path for path, _flags in self.system.opened.values()])

    def test_a_device_plugged_in_while_input_is_away_is_taken_with_the_others(self):
        self.open()
        self.away = True
        self.turn()
        self.system.names["/dev/input/event30"] = "USB Keyboard"
        self.hooks._scan()
        self.assertIn("/dev/input/event30", self.grabbed_paths())

    def test_a_device_that_can_only_be_read_is_still_read(self):
        self.system.read_only = {KEYBOARD}
        self.open()
        self.assertIn(KEYBOARD, self.hooks._paths.values())

    def test_touchpad_options_come_from_the_environment(self):
        self.assertEqual(capture.touchpad_options({}), (True, False))
        self.assertEqual(capture.touchpad_options({"BEAMER_TOUCHPAD": "natural"}), (True, True))
        self.assertEqual(capture.touchpad_options({"BEAMER_TOUCHPAD": " NoTap , natural"}), (False, True))
        self.assertEqual(capture.max_grab_seconds({"BEAMER_GRAB_MAX_SECONDS": "5"}), 5.0)
        for raw in ("", "0", "-3", "soon"):
            self.assertIsNone(capture.max_grab_seconds({"BEAMER_GRAB_MAX_SECONDS": raw}))


class GrabTest(Rig):
    def test_the_devices_are_grabbed_while_input_is_away_and_only_then(self):
        self.open()
        self.turn()
        self.assertEqual((self.grabbed_paths(), self.hooks.grabbed, self.guards), ([], False, []))
        self.away = True
        self.turn()
        self.assertEqual((self.grabbed_paths(), self.hooks.grabbed), ([KEYBOARD, MOUSE, TOUCHPAD], True))
        self.away = False
        self.turn()
        self.assertEqual((self.grabbed_paths(), self.hooks.grabbed), ([], False))

    def test_the_guard_is_told_before_the_first_device_is_taken(self):
        self.open()
        order = []
        real = self.system.grab
        self.system.grab = lambda fd, on: (order.append("grab"), real(fd, on))[1]

        def guard():
            made = FakeGuard()
            made.beat = lambda grabbed: order.append(("guard", grabbed))
            return made

        self.hooks._guard_factory = guard
        self.away = True
        self.turn()
        self.assertEqual(order[0], ("guard", True))
        self.assertEqual(order.count("grab"), 3)

    def test_without_a_guard_the_keyboard_is_not_taken_and_input_comes_home(self):
        self.open()
        self.guard_error = OSError("no interpreter")
        self.away = True
        with self.assertLogs(capture.LOGGER, "ERROR"):
            self.turn()
            self.turn()
        self.assertEqual(self.grabbed_paths(), [])
        self.assertEqual(len(self.failures), 1)
        self.assertIn("guard", self.sentences()[0])

    def test_a_guard_that_died_is_replaced_before_the_next_grab(self):
        self.open()
        self.away = True
        self.turn()
        self.away = False
        self.turn()
        self.guards[0].dead = True
        self.away = True
        self.turn()
        self.assertEqual((len(self.guards), self.guards[0].closed, self.guards[1].beats), (2, True, [True]))
        self.assertEqual(len(self.grabbed_paths()), 3)

    def test_a_device_another_program_holds_is_left_to_it_and_the_rest_are_taken(self):
        # A remapper has the keyboard: the desktop gets nothing from it either.
        self.open()
        self.system.busy = {KEYBOARD}
        self.away = True
        self.turn()
        self.assertEqual((self.grabbed_paths(), self.failures), ([MOUSE, TOUCHPAD], []))

    def test_a_device_that_cannot_be_taken_for_another_reason_means_none_is(self):
        # Taken in part, what is typed would go to the peer and to this machine's windows at once.
        self.open()
        self.system.broken = {MOUSE}
        self.away = True
        with self.assertLogs(capture.LOGGER, "WARNING"):
            self.turn()
            self.turn()
        self.assertEqual((self.grabbed_paths(), self.hooks.grabbed), ([], False))
        self.assertEqual(len(self.failures), 1)
        self.assertIn(MOUSE, self.sentences()[0])
        self.assertIs(self.guards[-1].beats[-1], False)

    def test_a_finger_resting_on_the_touchpad_is_not_a_key_held(self):
        self.open()
        self.system.down[TOUCHPAD] = {0x14A, 0x145}   # BTN_TOUCH, BTN_TOOL_FINGER
        self.away = True
        self.turn()
        self.assertEqual(len(self.grabbed_paths()), 3)
        self.assertEqual([entry for entry in self.system.log if entry[0] == "release"], [])

    def test_the_guard_is_told_the_devices_are_free_as_soon_as_they_are(self):
        self.open()
        self.away = True
        self.turn()
        order = []
        real = self.system.grab
        self.system.grab = lambda fd, on: (order.append("ungrab"), real(fd, on))[1]
        self.guards[-1].beat = lambda grabbed: order.append(("guard", grabbed))
        self.hooks.on_failure = lambda error: order.append("app told")
        with self.assertLogs(capture.LOGGER, "WARNING"):
            self.hooks._force_home("a test")
        self.assertEqual(order, ["ungrab"] * 3 + [("guard", False), "app told"])

    def test_a_key_pressed_while_the_devices_were_being_taken_is_let_go_for_the_desktop_too(self):
        # It went down after the look at what was held and before its device was taken: the
        # desktop saw the press. The device is given back for the moment it takes to write the
        # release, or the desktop would repeat the key for as long as input is away.
        self.open()
        real = self.system.grab
        pressed = []

        def grab(fd, on):
            real(fd, on)
            if on and not pressed and self.system.path_of(fd) == KEYBOARD:
                pressed.append(True)
                self.system.down[KEYBOARD] = {KEY_A}

        self.system.grab = grab
        self.away = True
        self.turn()
        self.assertEqual([entry for entry in self.system.log if entry[1] == KEYBOARD],
                         [("grab", KEYBOARD, True), ("grab", KEYBOARD, False), ("release", KEYBOARD, KEY_A), ("grab", KEYBOARD, True)])
        self.assertEqual(self.system.down[KEYBOARD], set())
        self.assertEqual(len(self.grabbed_paths()), 3)

    def test_a_key_that_types_is_waited_for_briefly_before_the_grab(self):
        self.open()
        self.system.down[KEYBOARD] = {KEY_A}
        self.away = True
        self.turn(0.1)
        self.assertEqual(self.grabbed_paths(), [])
        self.system.down[KEYBOARD] = set()
        self.turn(0.1)
        self.assertEqual(len(self.grabbed_paths()), 3)

    def test_a_key_never_let_go_does_not_hold_the_grab_off_for_ever(self):
        self.open()
        self.system.down[KEYBOARD] = {KEY_A}
        self.away = True
        self.turn(0.1)
        self.turn(capture.GRAB_WAIT_SECONDS)
        self.assertEqual(len(self.grabbed_paths()), 3)

    def test_a_modifier_held_is_not_waited_for(self):
        self.open()
        self.system.down[KEYBOARD] = {CTRL_R}   # the shortcut's own key, in the hold style
        self.away = True
        self.turn()
        self.assertEqual(len(self.grabbed_paths()), 3)


class HeldThroughTest(Rig):
    """A key held when the grab is taken. Grabbed, the device would never show the desktop its
    release, and the desktop would repeat the key for as long as input is away. So the release is
    written to the device before it is taken: the desktop reads it as it reads the key itself."""

    def grab_holding(self, path, code):
        self.open()
        self.system.down[path] = {code}
        self.feed(("key" if path == KEYBOARD else "button", self.device(path), code, True))
        self.away = True
        self.turn()
        self.turn(capture.GRAB_WAIT_SECONDS)  # a button is waited for as a key that types is
        self.assertTrue(self.hooks.grabbed)
        self.keys.clear()
        self.mouse.clear()

    def test_a_held_key_is_let_go_on_its_device_before_the_device_is_taken(self):
        for path, code in ((KEYBOARD, KEY_A), (KEYBOARD, CTRL_R), (MOUSE, BTN_LEFT)):
            with self.subTest(code=code):
                self.setUp()
                self.grab_holding(path, code)
                mine = [entry for entry in self.system.log if entry[1] == path]
                self.assertEqual(mine, [("release", path, code), ("grab", path, True)])
                # Not taken on trust: the kernel is asked again once everything is grabbed.
                self.assertEqual(self.system.down[path], set())
                self.hooks._end()

    def test_the_release_beamer_wrote_is_the_keys_release_for_the_bridge_too(self):
        self.grab_holding(KEYBOARD, KEY_A)
        self.feed(("key", self.device(KEYBOARD), KEY_A, False))
        self.assertEqual([key[:2] for key in self.keys], [("a", False)])

    def test_the_keyboard_saying_again_that_the_key_is_held_presses_nothing_anywhere(self):
        # A laptop's keyboard repeats a held key itself; after the release Beamer wrote, the kernel
        # takes the next of those for a new press. It is the same hold, and neither it nor its
        # release, when the finger lifts at last, is a key for the peer.
        self.grab_holding(KEYBOARD, KEY_A)
        device = self.device(KEYBOARD)
        self.feed(("key", device, KEY_A, False))
        self.keys.clear()
        self.now += 0.25
        self.feed(("key", device, KEY_A, True))
        self.now += 5.0
        self.feed(("key", device, KEY_A, False))
        self.assertEqual(self.keys, [])
        # The next press is the user's.
        self.feed(("key", device, KEY_A, True))
        self.assertEqual([key[:2] for key in self.keys], [("a", True)])

    def test_a_mouse_never_says_its_button_again_so_the_next_click_is_a_click(self):
        # Dragging as input leaves: the click that follows on the peer, however soon, is real.
        self.grab_holding(MOUSE, BTN_LEFT)
        device = self.device(MOUSE)
        self.feed(("button", device, BTN_LEFT, False))
        self.mouse.clear()
        self.feed(("button", device, BTN_LEFT, True), ("button", device, BTN_LEFT, False))
        self.assertEqual(len(self.mouse), 2)

    def test_the_watchdog_letting_go_while_a_late_key_is_dealt_with_leaves_nothing_taken(self):
        # The capture thread stalls between giving a device back and taking it again; the
        # watchdog lets go of everything meanwhile. Nothing may be taken after it.
        self.open()
        real = self.system.grab
        state = {"pressed": False}

        def grab(fd, on):
            real(fd, on)
            if on and not state["pressed"] and self.system.path_of(fd) == KEYBOARD:
                state["pressed"] = True
                self.system.down[KEYBOARD] = {KEY_A}

        def release(fd, code, real_release=self.system.release):
            real_release(fd, code)
            if state["pressed"]:
                # As the watchdog would, were the lock free: it must wait, so it is run where the
                # lock is known to be held, and seen not to get in.
                self.assertFalse(self.hooks._lock.acquire(blocking=False))

        self.system.grab, self.system.release = grab, release
        self.away = True
        self.turn()
        self.assertTrue(self.hooks.grabbed)
        self.hooks._let_go_from_outside()
        self.assertEqual(self.system.grabs, set())

    def test_nothing_is_taken_again_once_everything_was_let_go_from_outside(self):
        self.open()
        self.away = True
        self.turn()
        self.hooks._let_go_from_outside()
        self.system.down[KEYBOARD] = {KEY_A}
        self.assertIsNone(self.hooks._release_again({KEYBOARD}))
        self.assertEqual(self.system.grabs, set())

    def test_a_keyboard_that_does_not_repeat_does_not_cost_the_next_press(self):
        # A USB keyboard says nothing more of a held key, and the kernel drops its release.
        self.grab_holding(KEYBOARD, KEY_A)
        self.now += capture.GHOST_SECONDS + 0.1
        self.feed(("key", self.device(KEYBOARD), KEY_A, True))
        self.assertEqual([key[:2] for key in self.keys], [("a", True)])

    def test_a_shift_held_since_before_the_grab_still_counts_for_the_way_home(self):
        self.grab_holding(KEYBOARD, capture.KEY_LEFTSHIFT)
        device = self.device(KEYBOARD)
        self.feed(("key", device, capture.KEY_LEFTSHIFT, False), ("key", device, capture.KEY_LEFTSHIFT, True),
                  ("key", device, capture.KEY_RIGHTSHIFT, True))
        with self.assertLogs(capture.LOGGER, "WARNING"):
            self.feed(("key", device, capture.KEY_ESC, True))
        self.assertFalse(self.hooks.grabbed)

    def test_home_again_before_the_keyboard_repeats_the_key_is_the_desktops_and_the_bridges(self):
        self.grab_holding(KEYBOARD, KEY_A)
        self.away = False
        self.turn()
        self.feed(("key", self.device(KEYBOARD), KEY_A, True))
        self.assertEqual([key[:2] for key in self.keys], [("a", True)])

    def test_nothing_is_written_to_a_device_when_the_grab_ends(self):
        self.grab_holding(KEYBOARD, CTRL_R)
        before = len(self.system.log)
        self.away = False
        self.turn()
        self.assertEqual(self.system.log[before:], [("grab", path, False) for path in (KEYBOARD, MOUSE, TOUCHPAD)])

    def test_a_device_that_cannot_be_written_to_is_said_and_the_grab_is_taken(self):
        self.system.read_only = {KEYBOARD}
        self.open()
        self.system.down[KEYBOARD] = {CTRL_R}
        self.away = True
        with self.assertLogs(capture.LOGGER, "WARNING") as logs:
            self.turn()
        self.assertTrue(self.hooks.grabbed)
        self.assertIn("read only", "".join(logs.output))

    def test_a_key_the_kernel_keeps_as_held_is_said_and_tried_no_more_than_twice(self):
        self.system.deaf = {KEYBOARD}
        self.open()
        self.system.down[KEYBOARD] = {CTRL_R}
        self.away = True
        with self.assertLogs(capture.LOGGER, "WARNING"):
            self.turn()
        self.assertTrue(self.hooks.grabbed)
        self.assertEqual(self.system.log.count(("release", KEYBOARD, CTRL_R)), 2)


class FrenchTest(Rig):
    """An AZERTY keyboard, read through libxkbcommon's own fr layout: what AltGr and the dead keys
    type goes as text to a peer that types text, and as before to one that does not."""

    ALT_GR, A_GRAVE, E, CIRCUMFLEX, X, RIGHT_PAREN, ENTER, SPACE = 100, 11, 18, 26, 45, 12, 28, 57

    def setUp(self):
        super().setUp()
        try:
            import xkb_x11

            self.keymap = xkb_x11.Keymap.from_names("fr")
        except (RuntimeError, OSError) as exc:
            self.skipTest(f"needs libxkbcommon and the fr layout: {exc}")
        self.text = True
        self.hooks.text_while(lambda: self.text)
        self.open()
        self.board = self.device(KEYBOARD)

    def tap(self, *codes):
        """Each key down in turn, then up in reverse, as a chord is played."""
        for code in codes:
            self.feed(("key", self.board, code, True))
        for code in reversed(codes):
            self.feed(("key", self.board, code, False))

    def told(self):
        """What the bridge was told, text as ("text", ...) and a key as (name, down)."""
        found = [("text", key[0].text) if isinstance(key[0], capture.TextInput) else key[:2] for key in self.keys]
        self.keys.clear()
        return found

    def test_altgr_and_a_key_is_the_character_as_text_and_the_keys_release_says_nothing(self):
        self.tap(self.ALT_GR, self.A_GRAVE)
        self.assertEqual(self.told(), [("alt_r", True), ("text", "@"), ("alt_r", False)])

    def test_shift_and_altgr_reach_the_fourth_level(self):
        self.tap(SHIFT, self.ALT_GR, self.RIGHT_PAREN)
        self.assertEqual(self.told(), [("shift", True), ("alt_r", True), ("text", "¿"), ("alt_r", False), ("shift", False)])

    def test_text_carries_the_keys_code_for_the_list_of_keys_that_stay_here(self):
        self.tap(self.ALT_GR, self.A_GRAVE)
        self.assertEqual([key[2] for key in self.keys if isinstance(key[0], capture.TextInput)], [self.A_GRAVE])

    def test_a_peer_that_does_not_type_text_gets_the_keys_as_before(self):
        self.text = False
        self.tap(self.ALT_GR, self.A_GRAVE)
        self.assertEqual(self.told(), [("alt_r", True), ("à", True), ("à", False), ("alt_r", False)])

    def test_a_shortcut_with_altgr_stays_keys(self):
        self.tap(CTRL_R, self.ALT_GR, self.A_GRAVE)
        self.assertEqual(self.told(), [("cmd_r", True), ("alt_r", True), ("à", True), ("à", False), ("alt_r", False), ("cmd_r", False)])

    def test_a_key_with_nothing_more_under_altgr_stays_a_key(self):
        self.tap(self.ALT_GR, self.SPACE)
        self.assertEqual(self.told(), [("alt_r", True), ("space", True), ("space", False), ("alt_r", False)])

    def test_a_dead_key_and_a_letter_are_the_accented_letter_as_text(self):
        self.tap(self.CIRCUMFLEX)
        self.assertEqual(self.told(), [])
        self.tap(self.E)
        self.assertEqual(self.told(), [("text", "ê")])

    def test_shift_between_the_dead_key_and_the_letter_does_not_lose_the_accent(self):
        self.tap(SHIFT, self.CIRCUMFLEX)   # the diaeresis
        self.tap(SHIFT, self.E)
        self.assertEqual(self.told(), [("shift", True), ("shift", False), ("shift", True), ("text", "Ë"), ("shift", False)])

    def test_a_dead_key_before_a_letter_it_does_not_go_on_before_a_space_and_twice(self):
        for after, text in ((self.X, "^x"), (self.SPACE, "^"), (self.CIRCUMFLEX, "^")):
            self.tap(self.CIRCUMFLEX)
            self.tap(after)
            self.assertEqual(self.told(), [("text", text)])

    def test_a_named_key_after_a_dead_key_drops_the_accent(self):
        self.tap(self.CIRCUMFLEX)
        self.tap(self.ENTER)
        self.tap(self.E)
        self.assertEqual(self.told(), [("enter", True), ("enter", False), ("e", True), ("e", False)])

    def test_a_dead_key_for_a_peer_that_does_not_type_text_is_dropped_as_before(self):
        self.text = False
        self.tap(self.CIRCUMFLEX)
        self.tap(self.E)
        self.assertEqual(self.told(), [("e", True), ("e", False)])

    def test_an_accent_waiting_does_not_outlive_a_grab_the_watchdog_ended(self):
        self.away = True
        self.turn()
        self.tap(self.CIRCUMFLEX)
        self.hooks._let_go_from_outside()
        self.away = False
        self.turn()
        self.away = True
        self.turn()
        self.tap(self.E)
        self.assertEqual(self.told(), [("e", True), ("e", False)])

    def test_an_accent_waiting_does_not_outlive_the_grab(self):
        self.tap(self.CIRCUMFLEX)
        self.away = True
        self.turn()
        self.away = False
        self.turn()
        self.tap(self.E)
        self.assertEqual(self.told(), [("e", True), ("e", False)])


class UsRightAltTest(Rig):
    def test_right_alt_on_a_layout_with_no_third_level_is_plain_alt(self):
        try:
            import xkb_x11

            self.keymap = xkb_x11.Keymap.from_names("us")
        except (RuntimeError, OSError) as exc:
            self.skipTest(f"needs libxkbcommon: {exc}")
        self.hooks.text_while(lambda: True)
        self.open()
        device = self.device(KEYBOARD)
        self.feed(("key", device, 100, True), ("key", device, 18, True))
        self.assertEqual([key[:2] for key in self.keys], [("alt_r", True), ("e", True)])


class WaysHomeTest(Rig):
    def away_and_grabbed(self):
        self.open()
        self.away = True
        self.turn()
        self.assertTrue(self.hooks.grabbed)

    def test_both_shifts_and_esc_let_go_at_once_and_say_so(self):
        self.away_and_grabbed()
        device = self.device(KEYBOARD)
        with self.assertLogs(capture.LOGGER, "WARNING"):
            self.feed(("key", device, SHIFT, True), ("key", device, SHIFT_R, True), ("key", device, ESC, True))
        self.assertEqual((self.grabbed_paths(), self.sentences()), ([], [capture.PANIC]))
        # The app has not brought input home yet: the devices are not taken again meanwhile.
        self.turn()
        self.assertEqual(self.grabbed_paths(), [])
        self.away = False
        self.turn()
        self.away = True
        self.turn()
        self.assertEqual(len(self.grabbed_paths()), 3)

    def test_the_chord_is_only_a_chord_while_input_is_away(self):
        self.open()
        device = self.device(KEYBOARD)
        self.feed(("key", device, SHIFT, True), ("key", device, SHIFT_R, True), ("key", device, ESC, True))
        self.assertEqual(self.failures, [])
        self.assertEqual(self.keys[-1][:2], ("esc", True))

    def test_the_chord_works_across_two_keyboards(self):
        self.system.names["/dev/input/event30"] = "USB Keyboard"
        self.away_and_grabbed()
        with self.assertLogs(capture.LOGGER, "WARNING"):
            self.feed(("key", self.device(KEYBOARD), SHIFT, True), ("key", self.device("/dev/input/event30"), SHIFT_R, True),
                      ("key", self.device(KEYBOARD), ESC, True))
        self.assertEqual(self.grabbed_paths(), [])

    def test_stop_lets_go(self):
        self.away_and_grabbed()
        self.hooks.stop()
        self.assertEqual(self.grabbed_paths(), [])

    def test_every_way_out_lets_go_of_every_device_whatever_the_flag_says(self):
        # The flag and the kernel disagreeing must never leave a device held.
        for way in (self.hooks._end, self.hooks._let_go_from_outside, self.hooks._let_go):
            with self.subTest(way=way.__name__):
                self.setUp()
                self.away_and_grabbed()
                self.hooks._grabbed = False
                getattr(self.hooks, way.__name__)()
                self.assertEqual(self.grabbed_paths(), [])
                self.hooks._end()


class MaxSecondsTest(Rig):
    environ = {"BEAMER_GRAB_MAX_SECONDS": "5"}

    def test_the_grab_ends_after_the_time_given_whatever_the_app_says(self):
        self.open()
        self.away = True
        self.turn()
        self.turn(4.0)
        self.assertEqual(len(self.grabbed_paths()), 3)
        with self.assertLogs(capture.LOGGER, "WARNING"):
            self.turn(1.0)
        self.assertEqual(self.grabbed_paths(), [])
        self.assertEqual(self.sentences(), [capture.TOO_LONG.format(seconds=5.0)])
        self.turn(10.0)
        self.assertEqual(self.grabbed_paths(), [])


class EventsTest(Rig):
    def test_a_named_key_and_a_character_reach_the_bridge_as_x11s_do(self):
        self.open()
        device = self.device(KEYBOARD)
        self.feed(("key", device, 28, True), ("key", device, 28, False), ("key", device, KEY_A, True), ("key", device, KEY_A, False))
        self.assertEqual(self.keys, [("enter", True, 28, None), ("enter", False, 28, None),
                                     ("a", True, KEY_A, "a"), ("a", False, KEY_A, "a")])

    def test_shift_and_caps_lock_make_a_capital_and_the_release_names_the_same_key(self):
        self.open()
        device = self.device(KEYBOARD)
        self.feed(("key", device, SHIFT, True), ("key", device, KEY_A, True), ("key", device, SHIFT, False), ("key", device, KEY_A, False))
        self.assertEqual([key[0] for key in self.keys], ["shift", "A", "shift", "A"])
        self.keys.clear()
        self.system.led[KEYBOARD] = {capture.LED_CAPSL}
        self.feed(("key", device, KEY_A, True))
        self.assertEqual(self.keys[0][0], "A")

    def test_caps_lock_pressed_while_away_counts_though_no_led_shows_it(self):
        self.open()
        device = self.device(KEYBOARD)
        self.away = True
        self.turn()
        self.feed(("key", device, CAPS, True), ("key", device, CAPS, False), ("key", device, KEY_A, True), ("key", device, KEY_A, False))
        self.assertEqual(self.keys[-1][0], "A")
        # Home again, the LED is the truth once more.
        self.away = False
        self.turn()
        self.away = True
        self.turn()
        self.feed(("key", device, KEY_A, True))
        self.assertEqual(self.keys[-1][0], "a")

    def test_the_layout_and_its_group_are_the_ones_the_compositor_names_for_that_keyboard(self):
        self.open()
        asked = []
        with mock.patch.object(desktop_wlroots, "keyboard", lambda name: asked.append(name) or {"layout": "us,fr", "group": 1}):
            self.feed(("key", self.device(KEYBOARD), KEY_A, True))
            self.feed(("key", self.device(KEYBOARD), KEY_A, False), ("key", self.device(KEYBOARD), KEY_A, True))
        self.assertEqual((self.keys[0][0], asked), ("q", ["AT Translated Set 2 keyboard"]))

    def test_a_key_that_types_nothing_and_a_layout_that_cannot_be_read_send_nothing(self):
        self.open()
        self.feed(("key", self.device(KEYBOARD), KEY_Q, True))
        self.assertEqual(self.keys, [])
        self.hooks._keymap_factory = mock.Mock(side_effect=RuntimeError("no libxkbcommon"))
        self.hooks._names = None
        self.hooks._said_at = 0.0
        with mock.patch.object(linux_layout, "sources", lambda said=None: iter([("a test", linux_layout.Names("zz"))])), \
                self.assertLogs(capture.LOGGER, "ERROR"):
            self.feed(("key", self.device(KEYBOARD), KEY_A, True), ("key", self.device(KEYBOARD), KEY_A, False))
        self.assertEqual(self.keys, [])
        self.feed(("key", self.device(KEYBOARD), 28, True))
        self.assertEqual(self.keys[0][0], "enter")

    def test_buttons_are_the_bridges_mouse_messages(self):
        self.open()
        device = self.device(MOUSE)
        self.feed(("button", device, BTN_LEFT, True), ("button", device, BTN_LEFT, False), ("button", device, BTN_SIDE, True),
                  ("button", device, 0x120, True))
        self.assertEqual([capture_win.mouse_event(message, data) for message, _x, _y, data in self.mouse],
                         [("button", "left", True), ("button", "left", False), ("button", "back", True)])

    def test_a_finger_moving_on_the_touchpad_is_reported_at_home_as_it_is_away(self):
        # What libinput sends for one finger drawing on a touchpad, nothing grabbed: the sender
        # measures the push on a screen edge from these, as it does from capture_x11's.
        desktop_wlroots.use(TellsCursor())
        self.open()
        device = self.device(TOUCHPAD)
        self.feed(("motion", device, 5.0, 5.0))
        self.now += 0.02
        self.feed(("motion", device, -2.0, 3.0))
        self.assertEqual(self.moves, [(5, 5), (-2, 3)])
        self.assertEqual(self.system.grabs, set())
        self.away = True
        self.turn()
        self.feed(("motion", device, 4.0, 0.0))
        self.assertEqual(self.moves[-1], (4, 0))

    def test_at_home_moves_are_added_up_and_told_sixty_times_a_second_at_most(self):
        desktop_wlroots.use(TellsCursor())
        self.open()
        device = self.device(TOUCHPAD)
        self.feed(*[("motion", device, 1.0, 2.0)] * 5)
        self.assertEqual(self.moves, [(1, 2)])
        self.now += 0.02
        self.feed(("motion", device, 1.0, 2.0))
        self.assertEqual(self.moves, [(1, 2), (5, 10)])

    def test_away_every_move_is_told(self):
        self.open()
        device = self.device(TOUCHPAD)
        self.away = True
        self.turn()
        self.feed(*[("motion", device, 1.0, 2.0)] * 5)
        self.assertEqual(self.moves, [(1, 2)] * 5)

    def test_where_the_compositor_cannot_say_where_the_pointer_is_moves_at_home_are_not_told(self):
        # sway and the like: the sender would measure a push against a pointer that never moves.
        self.open()
        device = self.device(TOUCHPAD)
        self.feed(("motion", device, 5.0, 5.0))
        self.assertEqual(self.moves, [])
        self.away = True
        self.turn()
        self.feed(("motion", device, 4.0, 0.0))
        self.assertEqual(self.moves, [(4, 0)])

    def test_whoever_asks_for_every_move_at_home_has_them_with_no_compositor_to_ask(self):
        # tools/grab_probe.py over SSH: no compositor's socket in that shell, and the moves are
        # what it is run to count.
        self.hooks.home_moves = lambda: True
        self.open()
        self.feed(("motion", self.device(TOUCHPAD), 5.0, 5.0))
        self.assertEqual(self.moves, [(5, 5)])

    def test_moves_that_arrived_together_are_told_as_one(self):
        # A mouse that reports a thousand times a second, read by a loop that was kept waiting:
        # one message for the other machine, not one for each report.
        self.open()
        self.away = True
        self.turn()
        mouse = self.device(MOUSE)
        self.hooks._take([("motion", mouse, 3.0, 1.0), ("motion", mouse, 2.0, -4.0), ("motion", mouse, 1.0, 1.0)])
        self.assertEqual(self.moves, [(6, -2)])

    def test_a_click_between_two_moves_stays_between_them(self):
        self.open()
        self.away = True
        self.turn()
        mouse = self.device(MOUSE)
        order = []
        self.hooks._on_motion = lambda dx, dy: order.append(("move", dx, dy))
        self.hooks._on_mouse = lambda *event: order.append("button") or True
        self.hooks._take([("motion", mouse, 1.0, 0.0), ("motion", mouse, 2.0, 0.0), ("button", mouse, BTN_LEFT, True),
                          ("motion", mouse, 5.0, 0.0), ("button", mouse, BTN_LEFT, False)])
        self.assertEqual(order, [("move", 3, 0), "button", ("move", 5, 0), "button"])

    def test_an_event_that_cannot_be_passed_on_does_not_lose_the_ones_after_it(self):
        self.open()
        self.away = True
        self.turn()
        mouse = self.device(MOUSE)
        with mock.patch.object(self.hooks, "_button", side_effect=RuntimeError("the app refused")), \
                self.assertLogs(capture.LOGGER, "ERROR"):
            self.hooks._take([("button", mouse, BTN_LEFT, True), ("motion", mouse, 4.0, 0.0)])
        self.assertEqual(self.moves, [(4, 0)])

    def test_fractions_of_a_move_add_up(self):
        self.open()
        device = self.device(TOUCHPAD)
        self.away = True
        self.turn()
        self.feed(("motion", device, 0.4, -0.4), ("motion", device, 0.4, -0.4), ("motion", device, 0.4, -0.4), ("motion", device, 3.0, 0.0))
        self.assertEqual(self.moves, [(1, -1), (3, 0)])

    def test_an_event_beamer_sends_nowhere_is_passed_over(self):
        self.open()
        self.feed(("other", self.device(TOUCHPAD), 800))
        self.assertEqual((self.keys, self.mouse, self.moves), ([], [], []))

    def test_a_wheel_click_and_two_fingers_on_a_touchpad_scroll_the_same_way(self):
        self.open()
        # libinput: positive is down and right. A click down, then right.
        self.feed(("wheel", self.device(MOUSE), 120.0, 0.0), ("wheel", self.device(MOUSE), 0.0, 120.0))
        self.assertEqual([capture_win.mouse_event(message, data) for message, _x, _y, data in self.mouse],
                         [("wheel", -1.0, 0.0), ("wheel", 0.0, 1.0)])
        self.mouse.clear()
        # Two fingers: a third of a click at a time, upwards.
        for _ in range(3):
            self.feed(("scroll", self.device(TOUCHPAD), -capture.SCROLL_PER_CLICK / 3, 0.0))
        total = sum(capture_win.mouse_event(message, data)[1] for message, _x, _y, data in self.mouse)
        self.assertAlmostEqual(total, 1.0, places=1)


class ThreadTest(Rig):
    def wait(self, condition, what):
        deadline = time.monotonic() + 3
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.005)

    def setUp(self):
        super().setUp()
        self.hooks._clock = time.monotonic
        self.addCleanup(self.hooks.stop)

    def test_start_reads_the_devices_and_says_ready_and_stop_closes_everything(self):
        self.hooks.start()
        self.assertEqual(self.rows, [("ready", "", False)])
        context = self.contexts[-1]
        context.feed(("key", 1, 28, True))
        self.wait(lambda: self.keys, "the key")
        self.away = True
        self.wait(lambda: len(self.system.grabs) == 3, "the grab")
        self.wait(lambda: self.guards and self.guards[-1].beats.count(True) >= 2, "the guard's beats")
        self.hooks.stop()
        self.assertEqual((self.system.grabs, self.system.opened, context.closed, self.guards[-1].closed), (set(), {}, True, True))

    def test_no_permission_fails_the_start_with_the_sentence_and_ask_again_tries_again(self):
        self.system.denied = set(self.system.names)
        with self.assertRaises(capture.CaptureUnavailable) as raised:
            self.hooks.start()
        self.assertEqual(raised.exception.sentence, capture.NO_ACCESS)
        self.assertEqual(self.rows, [("unavailable", capture.NO_ACCESS, True)])
        self.system.denied = set()
        self.hooks.request_again()
        self.assertEqual(self.rows[-1], ("ready", "", False))

    def test_a_machine_with_no_device_at_all_and_one_without_libinput_say_so(self):
        self.system.names = {OWN: "Beamer virtual keyboard"}
        with self.assertRaises(capture.CaptureUnavailable) as raised:
            self.hooks.start()
        self.assertEqual(raised.exception.sentence, capture.NO_DEVICES)
        self.hooks._context_factory = mock.Mock(side_effect=RuntimeError("libinput is not installed"))
        with self.assertRaises(capture.CaptureUnavailable) as raised:
            self.hooks.start()
        self.assertIn("libinput is not installed", raised.exception.sentence)

    def test_a_stalled_capture_thread_has_the_devices_let_go_by_the_watchdog(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.hooks._on_key = lambda *key: release.wait(10)  # a callback that never returns
        with mock.patch.object(capture, "STALL_SECONDS", 0.2):
            self.hooks.start()
            self.away = True
            self.wait(lambda: len(self.system.grabs) == 3, "the grab")
            with self.assertLogs(capture.LOGGER, "ERROR"):
                self.contexts[-1].feed(("key", 1, 28, True))
                self.wait(lambda: not self.system.grabs, "the watchdog")
                self.wait(lambda: self.failures, "the app being told")
        self.assertEqual(self.sentences(), [capture.STALLED])
        # The devices are free, so the guard is told there is nothing to end Beamer for.
        self.assertIs(self.guards[-1].beats[-1], False)

    def test_a_guard_that_dies_while_the_devices_are_held_ends_the_capture_and_lets_go(self):
        self.hooks.start()
        self.away = True
        self.wait(lambda: len(self.system.grabs) == 3, "the grab")
        with self.assertLogs(capture.LOGGER, "ERROR"):
            self.guards[-1].dead = True
            self.wait(lambda: not self.system.grabs, "the devices let go")
            self.wait(lambda: self.failures, "the failure")
        self.assertEqual(self.rows[-1][0], "unavailable")

    def test_a_key_the_app_cannot_take_is_logged_and_the_capture_goes_on(self):
        calls = []

        def on_key(*key):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("the app tripped")
            return True

        self.hooks._on_key = on_key
        self.hooks.start()
        with self.assertLogs(capture.LOGGER, "ERROR"):
            self.contexts[-1].feed(("key", 1, 28, True))
            self.wait(lambda: calls, "the first key")
        self.contexts[-1].feed(("key", 1, 28, False))
        self.wait(lambda: len(calls) == 2, "the second key")
        self.assertEqual(self.failures, [])

    def test_the_watchdog_stays_free_while_the_app_is_told(self):
        release = threading.Event()
        self.addCleanup(release.set)
        told = threading.Event()
        self.hooks._on_key = lambda *key: release.wait(10)
        self.hooks.on_failure = lambda error: (told.set(), release.wait(10))  # the app's answer blocks too
        with mock.patch.object(capture, "STALL_SECONDS", 0.2):
            self.hooks.start()
            self.away = True
            self.wait(lambda: len(self.system.grabs) == 3, "the grab")
            with self.assertLogs(capture.LOGGER, "ERROR"):
                self.contexts[-1].feed(("key", 1, 28, True))
                self.wait(told.is_set, "the app being told")
            self.assertTrue(self.hooks._watchdog.is_alive())

    def test_an_error_in_the_loop_lets_go_and_tells_the_app(self):
        self.hooks.start()
        self.away = True
        self.wait(lambda: len(self.system.grabs) == 3, "the grab")
        with self.assertLogs(capture.LOGGER, "ERROR"):
            self.contexts[-1].events = mock.Mock(side_effect=RuntimeError("libinput broke"))
            self.wait(lambda: self.failures, "the failure")
        self.assertEqual(self.system.grabs, set())
        self.assertIn("libinput broke", self.sentences()[0])


if __name__ == "__main__":
    unittest.main()
