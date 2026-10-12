"""inject_uinput against fake devices and real keymaps: what a peer's keys, moves and scrolls become
as kernel events. The French and US layouts are compiled by libxkbcommon as the compositor compiles
them; those tests are skipped where it is not installed."""

from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import desktop_wlroots
import inject_uinput as inject
import linux_layout
from core.return_edge import Rect

SHIFT, CTRL, ALT_R = 42, 29, 100


class FakeDevices:
    def __init__(self, absolute=False):
        self.events = []
        self.absolute = object() if absolute else None
        self.leds = 0
        self.broken = False
        self.closed = False

    def _note(self, *event):
        if self.broken:
            raise OSError(19, "No such device")
        self.events.append(event)

    def key(self, code, down):
        self._note("key", code, down)

    def button(self, code, down):
        self._note("button", code, down)

    def move(self, dx, dy):
        self._note("move", dx, dy)

    def wheel(self, events):
        self._note("wheel", list(events))

    def move_to(self, across, down):
        self._note("move_to", across, down)

    def locks(self):
        return self.leds

    def close(self):
        self.closed = True


def real_keymap(layout):
    try:
        import xkb_x11

        return xkb_x11.Keymap.from_names(layout)
    except (RuntimeError, OSError) as exc:
        raise unittest.SkipTest(f"needs libxkbcommon and the {layout} layout: {exc}")


class Rig(unittest.TestCase):
    """The module with fake devices, no compositor socket, and the layout named by `layout`."""

    layout = "us"
    absolute = False

    def setUp(self):
        self.keymap = real_keymap(self.layout)
        self.devices = FakeDevices(self.absolute)
        self.made = 0
        self.said = []
        self.rows = []

        def connect():
            self.made += 1
            return self.devices

        self._reset()
        desktop_wlroots.use(None)
        for name, value in (("_connect", connect), ("_keymap", lambda said=None: (self.keymap, linux_layout.Names(self.layout), "a test")),
                            ("SETTLE_SECONDS", 0.0), ("on_problem", self.said.append),
                            ("on_permission", lambda *row: self.rows.append(row))):
            patcher = mock.patch.object(inject, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(linux_layout, "sources", lambda said=None: iter([("a test", linux_layout.Names(self.layout))]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._reset)
        self.addCleanup(desktop_wlroots.use, None)
        self.addCleanup(desktop_wlroots.set_mover, None)

    def _reset(self):
        inject._devices = inject._layout = inject._names = inject._problem = inject._said = None
        inject._said_at = 0.0
        inject._ready_at = inject._retry_at = 0.0
        inject._settled = False
        inject._stopped = False
        inject._state = inject.KeyState()
        inject._pressed.clear()
        inject._buttons_down.clear()
        inject._wheel_carry[:] = [0.0, 0.0, 0.0, 0.0]

    def keys(self):
        found = [(code, down) for kind, code, down in (event for event in self.devices.events if event[0] == "key")]
        self.devices.events.clear()
        return found


class NamedKeyTest(Rig):
    def test_a_named_key_is_its_evdev_code_and_a_repeat_is_left_to_the_compositor(self):
        inject.inject_key("enter", True)
        inject.inject_key("enter", True)
        inject.inject_key("enter", False)
        self.assertEqual(self.keys(), [(28, True), (28, False)])

    def test_left_and_right_modifiers_are_different_keys(self):
        for name, code in (("ctrl", 29), ("ctrl_r", 97), ("alt", 56), ("alt_r", 100), ("cmd", 125), ("shift_r", 54)):
            inject.inject_key(name, True)
            inject.inject_key(name, False)
            self.assertEqual(self.keys(), [(code, True), (code, False)], name)

    def test_alt_with_print_screen_is_the_kernels_sysrq_and_is_never_pressed(self):
        inject.inject_key("print_screen", True)
        inject.inject_key("print_screen", False)
        self.assertEqual(self.keys(), [(99, True), (99, False)])
        for alt in ("alt", "alt_r"):
            inject.inject_key(alt, True)
            with mock.patch.object(inject, "_warn_once") as warned:
                inject.inject_key("print_screen", True)
            warned.assert_called_once()
            inject.inject_key("b", True, "b")
            inject.inject_key("b", False, "b")
            inject.inject_key("print_screen", False)
            inject.inject_key(alt, False)
            pressed = [code for code, down in self.keys() if down]
            self.assertNotIn(99, pressed)
            self.assertEqual(len(pressed), 2)  # Alt and the letter: an ordinary Alt+B

    def test_an_unknown_name_presses_nothing(self):
        inject.inject_key("launch_app1", True)
        self.assertEqual(self.keys(), [])


class UsLayoutTest(Rig):
    layout = "us"

    def test_a_letter_and_a_capital(self):
        inject.inject_key("a", True, "a")
        inject.inject_key("a", False, "a")
        inject.inject_key("A", True, "a")
        inject.inject_key("A", False, "a")
        # The capital with nobody holding Shift: Shift is pressed round it, and it is let go at once
        # so the compositor's repeats do not come out without the Shift.
        self.assertEqual(self.keys(), [(30, True), (30, False), (SHIFT, True), (30, True), (30, False), (SHIFT, False), (30, False)])

    def test_caps_lock_as_the_keyboards_led_shows_it_is_allowed_for(self):
        self.devices.leds = inject.LOCK_MASK
        inject.inject_key("A", True, "a")
        self.assertEqual(self.keys()[0], (30, True))  # Caps Lock already makes it a capital

    def test_an_azerty_senders_characters_land_on_the_us_keys_that_type_them(self):
        # The AZERTY A key says "a", us place "q"; AZERTY's 1 key says "&".
        inject.inject_key("a", True, "q")
        inject.inject_key("a", False, "q")
        self.assertEqual(self.keys(), [(30, True), (30, False)])
        inject.inject_key("&", True, "1")
        inject.inject_key("&", False, "1")
        self.assertEqual(self.keys(), [(SHIFT, True), (8, True), (8, False), (SHIFT, False), (8, False)])

    def test_a_shortcut_goes_by_the_senders_character_and_the_peers_shift_is_left_alone(self):
        inject.inject_key("ctrl", True)
        inject.inject_key("a", True, "q")  # Ctrl+A typed on AZERTY stays Ctrl+A
        inject.inject_key("a", False, "q")
        inject.inject_key("ctrl", False)
        self.assertEqual(self.keys(), [(CTRL, True), (30, True), (30, False), (CTRL, False)])

    def test_a_character_this_layout_has_no_key_for_is_not_typed(self):
        import inject_x11

        # Said once in the life of the process, so the saying itself is what is checked.
        with mock.patch.object(inject_x11, "_warn_once") as warned:
            inject.inject_key("é", True, "2")
        self.assertEqual(self.keys(), [])
        warned.assert_called_once()


class FrenchLayoutTest(Rig):
    layout = "fr"

    def test_azerty_to_azerty_types_on_the_same_keys(self):
        for character, us, code in (("a", "q", 16), ("z", "w", 17), ("m", ";", 39), ("&", "1", 2), ("é", "2", 3), ("à", "0", 11), ("²", "`", 41), ("<", None, 86)):
            inject.inject_key(character, True, us)
            inject.inject_key(character, False, us)
            self.assertEqual(self.keys(), [(code, True), (code, False)], character)

    def test_a_digit_needs_shift_on_azerty(self):
        inject.inject_key("1", True, "1")
        inject.inject_key("1", False, "1")
        self.assertEqual(self.keys(), [(SHIFT, True), (2, True), (2, False), (SHIFT, False), (2, False)])

    def test_altgr_from_an_azerty_peer_is_the_same_two_keys_here(self):
        # AltGr+à as the wire carries it today: alt_r, then the key's own character.
        inject.inject_key("alt_r", True)
        inject.inject_key("à", True, "0")
        inject.inject_key("à", False, "0")
        inject.inject_key("alt_r", False)
        self.assertEqual(self.keys(), [(ALT_R, True), (11, True), (11, False), (ALT_R, False)])

    def test_text_lets_go_of_the_peers_modifiers_round_it(self):
        inject.inject_key("ctrl", True)
        self.keys()
        inject.inject_text("aé")
        self.assertEqual(self.keys(), [(CTRL, False), (16, True), (16, False), (3, True), (3, False), (CTRL, True)])


class LayoutTest(Rig):
    def test_the_compositor_is_asked_for_the_layout_once_a_second_not_at_every_character(self):
        asked = []
        with mock.patch.object(desktop_wlroots, "keyboard", lambda device: asked.append(device)):
            for _ in range(20):
                inject.inject_key("a", True, "a")
                inject.inject_key("a", False, "a")
            self.assertEqual(asked, ["Beamer virtual keyboard"])
            with mock.patch.object(inject.time, "monotonic", return_value=inject.time.monotonic() + inject.LAYOUT_SECONDS + 1):
                inject.inject_key("a", True, "a")
        self.assertEqual(len(asked), 2)

    def test_a_layout_that_cannot_be_compiled_is_tried_once_not_at_every_character(self):
        tried = []

        def broken(said=None):
            tried.append(True)
            raise RuntimeError("libxkbcommon is not installed")

        with mock.patch.object(inject, "_keymap", broken), mock.patch.object(inject, "_warn_once"):
            for _ in range(5):
                inject.inject_key("a", True, "a")
                inject.inject_key("a", False, "a")
        self.assertEqual((len(tried), self.keys()), (1, []))
        # Named keys need no layout and are still pressed.
        inject.inject_key("enter", True)
        self.assertEqual(self.keys(), [(28, True)])


class PointerTest(Rig):
    def test_moves_buttons_and_a_wheel_click(self):
        inject.inject_mouse_move(3, -4)
        inject.inject_mouse_move(0, 0)
        inject.inject_mouse_button("left", True)
        inject.inject_mouse_button("left", False)
        inject.inject_mouse_button("forward", True)
        inject.inject_mouse_button("thumb", True)
        inject.inject_scroll(1, 0, "line")
        self.assertEqual(self.devices.events, [
            ("move", 3, -4), ("button", 0x110, True), ("button", 0x110, False), ("button", 0x114, True),
            ("wheel", [(inject.REL_WHEEL_HI_RES, 120), (inject.REL_WHEEL, 1)]),
        ])

    def test_the_compositor_is_asked_once_to_leave_the_pointers_moves_unaccelerated(self):
        asked = []
        with mock.patch.object(desktop_wlroots, "plain_pointer", lambda *device: asked.append(device)):
            inject.inject_mouse_move(1, 1)
            inject.inject_mouse_move(1, 1)
        self.assertEqual(asked, [("Beamer virtual pointer", inject.VENDOR, 2)])

    def test_a_move_is_noted_for_a_compositor_that_does_not_say_where_the_pointer_is(self):
        desktop_wlroots.set_cursor_position(100, 100)
        inject.inject_mouse_move(5, 7)
        self.assertEqual(desktop_wlroots.cursor_position(), (105, 107))

    def test_a_trackpads_fine_scroll_stays_fine_and_adds_up_to_whole_clicks(self):
        carry = [0.0, 0.0, 0.0, 0.0]
        events = []
        for _ in range(8):
            events += inject.plan_wheel(0.125, 0, "line", carry)
        self.assertEqual(events, [(inject.REL_WHEEL_HI_RES, 15)] * 7 + [(inject.REL_WHEEL_HI_RES, 15), (inject.REL_WHEEL, 1)])
        self.assertEqual(inject.plan_wheel(-1, 2, "line", carry),
                         [(inject.REL_WHEEL_HI_RES, -120), (inject.REL_WHEEL, -1), (inject.REL_HWHEEL_HI_RES, 240), (inject.REL_HWHEEL, 2)])
        # 40 pixels are one click; less than a 120th is carried, not lost.
        self.assertEqual(inject.plan_wheel(40, 0, "pixel", [0.0] * 4), [(inject.REL_WHEEL_HI_RES, 120), (inject.REL_WHEEL, 1)])
        small = [0.0] * 4
        self.assertEqual([inject.plan_wheel(0.1, 0, "pixel", small) for _ in range(4)], [[], [], [], [(inject.REL_WHEEL_HI_RES, 1)]])

    def test_an_absolute_point_spans_every_display(self):
        displays = [Rect(0, 0, 1920, 1080), Rect(1920, -200, 2560, 1440)]
        self.assertEqual(inject.absolute_point(0, -200, displays), (0, 0))
        self.assertEqual(inject.absolute_point(4479, 1239, displays), (65535, 65535))
        self.assertEqual(inject.absolute_point(99999, 99999, displays), (65535, 65535))


class AbsoluteTest(Rig):
    absolute = True

    def test_with_no_compositor_socket_a_placement_goes_through_the_absolute_pointer(self):
        inject.start()
        with mock.patch.object(desktop_wlroots, "_screens", [Rect(0, 0, 1000, 500)]):
            desktop_wlroots.set_cursor_position(999, 0)
        self.assertEqual(self.devices.events, [("move_to", 65535, 0)])


class LifeTest(Rig):
    def test_start_makes_the_devices_once_and_says_ready(self):
        inject.start()
        inject.start()
        inject.inject_key("enter", True)
        self.assertEqual((self.made, self.rows, self.said), (1, [("ready", "", False)], []))

    def test_release_all_lets_go_of_everything_held(self):
        inject.inject_key("ctrl", True)
        inject.inject_key("a", True, "a")
        inject.inject_mouse_button("right", True)
        self.devices.events.clear()
        inject.release_all()
        self.assertEqual(sorted(self.devices.events), [("button", 0x111, False), ("key", 29, False), ("key", 30, False)])
        inject.release_all()
        self.assertEqual(len(self.devices.events), 3)
        # Nothing is remembered as held: the next press is a press.
        inject.inject_key("a", True, "a")
        self.assertEqual(self.devices.events[-1], ("key", 30, True))

    def test_stop_releases_closes_and_nothing_is_played_after(self):
        inject.inject_key("shift", True)
        inject.stop()
        self.assertEqual(self.devices.events, [("key", SHIFT, True), ("key", SHIFT, False)])
        self.assertTrue(self.devices.closed)
        inject.inject_key("enter", True)
        self.assertEqual(self.made, 1)

    def test_devices_the_kernel_took_away_are_made_again_later_not_at_every_event(self):
        inject.start()
        self.devices.broken = True
        inject.inject_mouse_move(1, 1)
        self.assertTrue(self.devices.closed)
        self.assertEqual(self.rows[-1][0], "unavailable")
        self.devices = FakeDevices()
        inject.inject_mouse_move(1, 1)
        self.assertEqual(self.made, 1)
        with mock.patch.object(inject.time, "monotonic", return_value=inject.time.monotonic() + inject.RETRY_SECONDS + 1):
            inject.inject_mouse_move(2, 2)
        self.assertEqual((self.made, self.devices.events), (2, [("move", 2, 2)]))

    def test_a_key_held_when_the_devices_went_is_not_thought_held_on_the_new_ones(self):
        closed = []
        self.keymap.close = lambda: closed.append(True)
        inject.inject_key("a", True, "a")
        inject.inject_key("enter", True)
        self.devices.broken = True
        inject.inject_mouse_move(1, 1)
        self.assertEqual(closed, [True])  # the layout's keymap is not leaked
        self.keymap = real_keymap(self.layout)
        self.devices = FakeDevices()
        with mock.patch.object(inject.time, "monotonic", return_value=inject.time.monotonic() + inject.RETRY_SECONDS + 1):
            # The peer still holds both: its repeats are presses on the new keyboard.
            inject.inject_key("enter", True)
            inject.inject_key("a", True, "a")
        self.assertEqual(self.keys(), [(28, True), (30, True)])


class NoDevicesTest(Rig):
    def fail_with(self, exc):
        def connect():
            self.made += 1
            raise exc

        patcher = mock.patch.object(inject, "_connect", connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_reason_has_its_sentence_said_once_and_no_event_raises(self):
        for exc, sentence in ((PermissionError(13, "Permission denied"), inject.NO_ACCESS),
                              (FileNotFoundError(2, "No such file"), inject.NO_UINPUT),
                              (ImportError("No module named 'evdev'"), inject.NO_EVDEV)):
            with self.subTest(sentence=sentence):
                self._reset()
                self.said.clear()
                self.rows.clear()
                self.made = 0
                self.fail_with(exc)
                with self.assertLogs(inject.LOGGER, "WARNING"):
                    inject.start()
                inject.inject_key("a", True, "a")
                inject.inject_mouse_move(1, 1)
                inject.inject_scroll(1)
                inject.release_all()
                self.assertEqual((self.said, self.rows, self.made), ([sentence], [("unavailable", sentence, True)], 1))

    def test_ask_again_tries_at_once(self):
        self.fail_with(PermissionError(13, "Permission denied"))
        with self.assertLogs(inject.LOGGER, "WARNING"):
            inject.start()
        inject.request_again()
        self.assertEqual(self.made, 2)


if __name__ == "__main__":
    unittest.main()
