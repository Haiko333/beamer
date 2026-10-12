"""Keyboard and mouse injection through /dev/uinput: the twin of inject_x11.py for a Wayland
desktop with no RemoteDesktop portal (Hyprland, sway, any compositor), with the same functions, so
the receiver drives it unchanged.

uinput and not the wlroots virtual-keyboard and virtual-pointer protocols: the kernel's devices are
an ordinary keyboard and mouse to every compositor there is, GNOME and KDE included, need no
Wayland client code, and keep working across a compositor restart. What they cost is a permission
(a udev rule opens /dev/uinput to the person at the seat; see the README) and that the keys are
typed in the compositor's own layout, which cannot be changed from here.

Three devices, made when the app starts, since a compositor takes a moment to adopt a new device
and the first keys sent to one it has not adopted are lost:

- a keyboard, pressed by evdev code, the codes the wire's names already are (keytable);
- a pointer for relative moves, buttons and the wheel, in the kernel's high-resolution wheel
  units so a trackpad's fine scroll stays fine; Hyprland and sway are asked to leave its moves
  unaccelerated, as XTest's and libei's are, so a peer's move of ten is ten here too;
- only where the compositor's socket cannot place the pointer, an absolute pointer that can.

A character is typed as inject_x11 types it, by the same planner, over the layout the compositor
says it has (linux_layout). Caps Lock and Num Lock are read off the virtual keyboard's own LEDs,
which the compositor sets as it does a real keyboard's: no compositor has to be asked.

Where Shift alone does not reach a character, the layout's other ways are used (xkb_levels): the
key with AltGr, as @ and € are on a French keyboard, or a dead key and then the letter, as ê is.
Such a key goes down and up at once: nothing is left held for a repeat or a release to find. A
character the layout has no way to at all is handed to `wtype`, where it is installed, which types
through the compositor's virtual-keyboard protocol with a layout of its own; with no wtype it is
not typed, and that is logged once.

Planning is pure and the devices are a seam (`_connect`), so all of it is tested with no kernel.
python-evdev is reached only when the devices are made, so this module imports anywhere."""

import functools
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Callable, List, Optional, Set, Tuple

from capture_x11 import LOCK_MASK, NUM_LOCK_MASK
from inject_portal import WaylandLayout, wayland_steps
from capture_x11 import KEYCODE_OFFSET
from input_injector import CHORD_KEYS, _by_place
from inject_x11 import NAME_TO_CODE, KeyState, _warn_once, plan_key, plan_text
import xkb_levels
import desktop_wlroots
import linux_layout

LOGGER = logging.getLogger(__name__)

# The capture reads every keyboard and mouse on the machine: it knows these by their names and
# leaves them be, or a key played for a peer would be sent straight back to it.
NAME_PREFIX = "Beamer virtual"
KEYBOARD_NAME = f"{NAME_PREFIX} keyboard"
POINTER_NAME = f"{NAME_PREFIX} pointer"
ABSOLUTE_NAME = f"{NAME_PREFIX} absolute pointer"
# Not a registered USB vendor: the bus is the kernel's "virtual" one.
BUS_VIRTUAL, VENDOR = 0x06, 0x0BEA
PRODUCTS = {KEYBOARD_NAME: 1, POINTER_NAME: 2, ABSOLUTE_NAME: 3}

# evdev: the keys a keyboard may have. 0x100 to 0x15f are buttons (mice, joysticks, gamepads), and
# a keyboard that claimed them would be taken for one of those.
KEY_CODES = tuple(range(1, 0x100)) + tuple(range(0x160, 0x300))
KEY_SYSRQ, ALT_CODES = 99, frozenset({56, 100})
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112, "back": 0x113, "forward": 0x114}
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_LED = 0, 1, 2, 3, 0x11
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0, 1, 6, 8, 11, 12
ABS_X, ABS_Y, ABS_MAX = 0, 1, 65535
LED_NUML, LED_CAPSL, LED_SCROLLL = 0, 1, 2
# The kernel counts a wheel click as 120; the PC reckons a click scrolls 40 pixels (inject_x11).
HI_RES_PER_CLICK = 120
PIXELS_PER_CLICK = 40.0
# How long the compositor gets to adopt the devices before the first event is played.
SETTLE_SECONDS = 0.3
# How long what the compositor said of the keyboard layout is gone by.
LAYOUT_SECONDS = 1.0
# After the devices could not be made, or stopped taking events, they are made again no sooner.
RETRY_SECONDS = 5.0

NO_EVDEV = ("Beamer cannot control this machine: python-evdev is not installed, so another machine cannot "
            "drive this one.")
NO_ACCESS = ("Beamer may not create a keyboard and mouse on this machine, so another machine cannot drive this "
             "one: /dev/uinput is not open to this user. Install Beamer's udev rule (packaging/linux in Beamer's "
             "source, described in the README), then choose Ask again.")
NO_UINPUT = ("Beamer cannot control this machine: /dev/uinput is missing. Load the kernel's uinput module "
             "(sudo modprobe uinput), then choose Ask again.")
FAILED = "Beamer cannot control this machine: {detail}"

# Called with one of the sentences above when the devices cannot be made; set by the app.
on_problem: Optional[Callable[[str], None]] = None
on_permission: Optional[Callable[[str, str, bool], None]] = None


def plan_wheel(dy, dx, mode: str, carry: List[float]) -> List[Tuple[int, int]]:
    """One scroll message as (REL code, value) events: the high-resolution axis in 120ths of a
    click, and the plain axis a whole click at a time for what reads only that. `carry` holds the
    fraction of a 120th and the 120ths not yet a click, for [y, x] each. The wire's y is positive
    away from the hand and its x positive right, which is the kernel's sense too."""
    scale = HI_RES_PER_CLICK / PIXELS_PER_CLICK if mode == "pixel" else float(HI_RES_PER_CLICK)
    events: List[Tuple[int, int]] = []
    for axis, (value, fine, plain) in enumerate(((dy, REL_WHEEL_HI_RES, REL_WHEEL), (dx, REL_HWHEEL_HI_RES, REL_HWHEEL))):
        carry[axis] += float(value) * scale
        whole = int(carry[axis])
        carry[axis] -= whole
        if not whole:
            continue
        events.append((fine, whole))
        carry[2 + axis] += whole
        clicks = int(carry[2 + axis] / HI_RES_PER_CLICK)
        carry[2 + axis] -= clicks * HI_RES_PER_CLICK
        if clicks:
            events.append((plain, clicks))
    return events


def absolute_point(x: int, y: int, displays) -> Tuple[int, int]:
    """A logical position as the absolute pointer's, whose range a compositor lays over the whole
    of its displays."""
    left, top = min(rect.x for rect in displays), min(rect.y for rect in displays)
    width = max(rect.x + rect.width for rect in displays) - left
    height = max(rect.y + rect.height for rect in displays) - top
    across = (x - left) * ABS_MAX // max(width - 1, 1)
    down = (y - top) * ABS_MAX // max(height - 1, 1)
    return min(max(across, 0), ABS_MAX), min(max(down, 0), ABS_MAX)


def problem_for(exc: BaseException) -> str:
    if isinstance(exc, ImportError):
        return NO_EVDEV
    if isinstance(exc, PermissionError):
        return NO_ACCESS
    if isinstance(exc, FileNotFoundError):
        return NO_UINPUT
    return FAILED.format(detail=exc)


class _Devices:
    """The uinput devices themselves."""

    def __init__(self, absolute: bool) -> None:
        from evdev import AbsInfo, UInput

        class Plain(UInput):
            """python-evdev opens the new device's own /dev/input node as well, to offer it for
            reading: that waits two seconds for a node this user may not open, then fails with the
            device already made and never closed. Nothing here reads that node (the LEDs come back
            on the uinput fd), so it is not looked for."""

            def _find_device(self, fd):
                return None

        UInput = Plain
        self._made = []
        try:
            self.keyboard = self._make(UInput, KEYBOARD_NAME, {
                EV_KEY: KEY_CODES, EV_LED: (LED_NUML, LED_CAPSL, LED_SCROLLL)})
            self.pointer = self._make(UInput, POINTER_NAME, {
                EV_KEY: tuple(BUTTONS.values()),
                EV_REL: (REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES)})
            self.absolute = None
            if absolute:
                span = AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)
                self.absolute = self._make(UInput, ABSOLUTE_NAME, {
                    EV_KEY: (BUTTONS["left"],), EV_ABS: ((ABS_X, span), (ABS_Y, span))})
        except Exception:
            self.close()
            raise
        self._leds: Set[int] = set()

    def _make(self, UInput, name: str, events: dict):
        device = UInput({kind: list(codes) for kind, codes in events.items()}, name=name, bustype=BUS_VIRTUAL,
                        vendor=VENDOR, product=PRODUCTS[name], phys="beamer")
        self._made.append(device)
        return device

    def key(self, code: int, down: bool) -> None:
        self.keyboard.write(EV_KEY, code, 1 if down else 0)
        self.keyboard.syn()

    def button(self, code: int, down: bool) -> None:
        self.pointer.write(EV_KEY, code, 1 if down else 0)
        self.pointer.syn()

    def move(self, dx: int, dy: int) -> None:
        if dx:
            self.pointer.write(EV_REL, REL_X, dx)
        if dy:
            self.pointer.write(EV_REL, REL_Y, dy)
        self.pointer.syn()

    def wheel(self, events: List[Tuple[int, int]]) -> None:
        for code, value in events:
            self.pointer.write(EV_REL, code, value)
        self.pointer.syn()

    def move_to(self, across: int, down: int) -> None:
        self.absolute.write(EV_ABS, ABS_X, across)
        self.absolute.write(EV_ABS, ABS_Y, down)
        self.absolute.syn()

    def locks(self) -> int:
        """The Caps Lock and Num Lock bits, from the LED events the compositor has written to the
        keyboard since the last look."""
        try:
            for event in self.keyboard.read():
                if event.type == EV_LED:
                    (self._leds.add if event.value else self._leds.discard)(event.code)
        except BlockingIOError:
            pass
        return (LOCK_MASK if LED_CAPSL in self._leds else 0) | (NUM_LOCK_MASK if LED_NUML in self._leds else 0)

    def close(self) -> None:
        for device in self._made:
            try:
                device.close()
            except Exception:
                LOGGER.exception("A virtual input device did not close cleanly")
        self._made = []


def _make_devices():
    # The absolute pointer only where nothing else places the pointer: a compositor may tie one to
    # a single display, and Hyprland and sway place it exactly through their sockets.
    return _Devices(absolute=not desktop_wlroots.compositor())


_connect = _make_devices
_keymap = linux_layout.keymap
_devices = None
_ready_at = 0.0
_retry_at = 0.0
_settled = False
_stopped = False
_retrying = False
_problem: Optional[str] = None
_layout: Optional[WaylandLayout] = None
_levels: Optional[xkb_levels.Levels] = None
_names = None
_said: Optional[dict] = None
_said_at = 0.0
_state = KeyState()
_pressed: Set[int] = set()
_buttons_down: Set[str] = set()
_wheel_carry = [0.0, 0.0, 0.0, 0.0]
_lock = threading.RLock()


def _locked(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _lock:
            return function(*args, **kwargs)

    return wrapper


def _report(status: str, sentence: str = "", retryable: bool = False) -> None:
    global _problem
    if sentence and sentence != _problem:
        LOGGER.warning("%s", sentence)
        if on_problem is not None:
            try:
                on_problem(sentence)
            except Exception:
                LOGGER.exception("Could not report an injection problem")
    _problem = sentence or None
    if on_permission is not None:
        try:
            on_permission(status, sentence, retryable)
        except Exception:
            LOGGER.exception("Could not report the injection permission state")


def _open():
    """The devices, made if they are not there and may be tried again; None when they cannot be."""
    global _devices, _ready_at, _retry_at, _settled
    if _devices is not None:
        return _devices
    now = time.monotonic()
    if _stopped or now < _retry_at:
        return None
    try:
        _devices = _connect()
    except Exception as exc:
        _retry_at = now + RETRY_SECONDS
        if not isinstance(exc, (ImportError, OSError)):
            LOGGER.exception("The virtual input devices could not be made")
        _report("unavailable", problem_for(exc), retryable=True)
        return None
    _ready_at = now + SETTLE_SECONDS
    _settled = False
    if getattr(_devices, "absolute", None) is not None:
        desktop_wlroots.set_mover(_move_to)
    _report("ready")
    return _devices


def _drop() -> None:
    """After a failed write the devices are closed and made again on a later event: a device the
    kernel took away never comes back."""
    global _devices, _retry_at
    dead, _devices = _devices, None
    _retry_at = time.monotonic() + RETRY_SECONDS
    desktop_wlroots.set_mover(None)
    _forget_layout()
    # Nothing is held on a device that is gone, and the planner must not think otherwise: it
    # would swallow the peer's repeats of a key the next device never got.
    _forget_held()
    if dead is not None:
        dead.close()


def _forget_layout() -> None:
    global _layout, _levels, _names, _said_at
    if _layout is not None:
        _layout.keymap.close()
    _layout, _levels, _names, _said_at = None, None, None, 0.0


def _forget_held() -> None:
    _pressed.clear()
    _buttons_down.clear()
    _state.mods_down.clear()
    _state.named_down.clear()
    _state.chars_down.clear()
    _wheel_carry[:] = [0.0, 0.0, 0.0, 0.0]


def _play(function) -> None:
    """Runs `function(devices)` under the lock once the compositor has had time to adopt them; a
    machine with no devices drops the event, having said why once."""
    global _settled
    with _lock:
        devices = _open()
        if devices is None:
            return
        wait = _ready_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        if not _settled:
            # Once the compositor has the pointer, and before its first move.
            _settled = True
            desktop_wlroots.plain_pointer(POINTER_NAME, VENDOR, PRODUCTS[POINTER_NAME])
        try:
            function(devices)
        except OSError as exc:
            LOGGER.warning("The virtual input devices stopped taking events (%s)", exc)
            _drop()
            _report("unavailable", FAILED.format(detail=exc), retryable=True)


def _typing() -> Tuple[Optional[WaylandLayout], int]:
    """(the layout to type in, its group typing now). The compositor is asked which at most once
    in LAYOUT_SECONDS, not at every character, and the layout is compiled again only when it names
    another one; one that cannot be compiled is not tried again until then either."""
    global _layout, _levels, _names, _said, _said_at
    now = time.monotonic()
    if now - _said_at >= LAYOUT_SECONDS or _said_at == 0.0:
        _said, _said_at = desktop_wlroots.keyboard(KEYBOARD_NAME), now
        wanted = next(linux_layout.sources(said=_said))[1]
        if wanted != _names:
            if _layout is not None:
                _layout.keymap.close()
            _layout, _levels, _names = None, None, wanted
            try:
                keymap, _used, origin = _keymap(said=_said)
            except Exception as exc:
                _warn_once("Characters cannot be typed: %s", str(exc))
            else:
                _layout, _levels = _Layout(keymap), xkb_levels.Levels(keymap, _on_keyboard)
                LOGGER.info("Typing in the keyboard layout %s, from %s", wanted.layout or "(default)", origin)
    return _layout, (_said or {}).get("group", 0)


def _press(devices, steps: List[Tuple[int, bool]]) -> None:
    for code, down in steps:
        if code not in _KEY_SET:
            _warn_once("No key on the virtual keyboard for evdev code %s", str(code))
            continue
        if code == KEY_SYSRQ and down and _pressed & ALT_CODES:
            # Alt with Print Screen is the kernel's own SysRq chord, which reads uinput's keys as
            # it reads a real keyboard's: the key after it could sync, reboot or power off this
            # machine from under a locked screen. No peer gets to press it.
            _warn_once("Alt+Print Screen is not pressed for a peer (%s): it is the kernel's SysRq", "print_screen")
            continue
        devices.key(code, down)
        (_pressed.add if down else _pressed.discard)(code)


_KEY_SET = frozenset(KEY_CODES)


def start() -> None:
    """Make the devices now, as the app starts, so they are adopted before a peer's first key and
    a missing permission is said while someone is at this machine."""
    global _stopped
    with _lock:
        _stopped = False
        _open()


def request_again() -> None:
    global _retry_at
    with _lock:
        _retry_at = 0.0
        _open()


def available() -> bool:
    """Whether a peer's input can be played here: the receiver tells no peer it may drive a
    machine that says no (core/receiver.py). A plain read, asked under the receiver's lock. Yes
    until the devices have been tried and could not be made; and since a machine no peer drives
    gets no input that would have them tried again, asking is what tries, off this thread."""
    global _retrying
    if _devices is not None:
        return True
    if _stopped:
        return False
    if _problem is None:
        return True
    if time.monotonic() >= _retry_at and not _retrying:
        _retrying = True
        threading.Thread(target=_retry, name="Beamer-uinput-retry", daemon=True).start()
    return False


def _retry() -> None:
    global _retrying
    try:
        with _lock:
            if not _stopped:
                _open()
    finally:
        _retrying = False


@_locked
def stop() -> None:
    """For good: every key is let go and the devices are removed."""
    global _devices, _stopped
    release_all()
    _stopped = True
    desktop_wlroots.set_mover(None)
    dead, _devices = _devices, None
    if dead is not None:
        dead.close()
    _forget_layout()


def _on_keyboard(keycode: int) -> bool:
    """Below evdev's 0x100 are the keys of a keyboard; above, buttons and the keys of phones and
    remote controls, which a layout may give a character but few programs expect one from."""
    code = keycode - KEYCODE_OFFSET
    return code in _KEY_SET and code < 0x100


class _Layout(WaylandLayout):
    """The layout's keys that the virtual keyboard has: xkb also puts characters on keys of
    remote controls and phones (a "#" far above any keyboard's), which cannot be pressed here."""

    def find(self, character: str, group: int, locks: int):
        found = super().find(character, group, locks)
        return found if found is not None and _on_keyboard(found[0]) else None


SHIFT_L, ALT_GR = 42, xkb_levels.KEY_RIGHTALT
# wtype is given this long: it types a character or a few and ends.
WTYPE_SECONDS = 1.0


def _type_out(text: str) -> bool:
    """Type `text` through wtype, for what this layout has no key for. False when there is no
    wtype or it failed. The text is one argument after `--`: nothing in it is read as an option."""
    path = shutil.which("wtype", path="/usr/bin:/bin:/usr/local/bin")
    if path is None or not os.environ.get("WAYLAND_DISPLAY"):
        return False
    try:
        return subprocess.run([path, "--", text.encode("utf-8")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=WTYPE_SECONDS).returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:  # ValueError: half a surrogate pair, a NUL
        LOGGER.warning("wtype did not type (%s)", exc)
        return False


def _strokes(character: str, layout, group: int, locks: int):
    return _levels.strokes(character, group, locks, lambda wanted: layout.find(wanted, group, locks))


def _stroke_events(strokes) -> List[Tuple[int, bool]]:
    """Each key down and up at once with Shift and AltGr as its level needs them, nothing else
    held: the caller has let go of what the peer holds."""
    events: List[Tuple[int, bool]] = []
    for keycode, shift, altgr in strokes:
        code = keycode - KEYCODE_OFFSET
        around = ([SHIFT_L] if shift else []) + ([ALT_GR] if altgr else [])
        events += [(held, True) for held in around] + [(code, True), (code, False)]
        events += [(held, False) for held in reversed(around)]
    return events


def _held_codes(names) -> List[int]:
    return [NAME_TO_CODE[name] for name in sorted(names) if name in NAME_TO_CODE]


def _beyond_shift(devices, text: str, layout, group: int, locks: int) -> None:
    """Type `text`, none of which Shift alone reaches, with nothing of the peer's held: by the
    layout's other levels and dead keys where it has them, and through wtype for the rest."""
    unkeyed = ""

    def flush() -> None:
        nonlocal unkeyed
        if unkeyed and not _type_out(unkeyed):
            for character in unkeyed:
                _warn_once("No key types %r in this layout, and there is no wtype to type it", character)
        unkeyed = ""

    for character in text:
        strokes = _strokes(character, layout, group, locks)
        if strokes is None:
            unkeyed += character
            continue
        flush()
        _press(devices, _stroke_events(strokes))
    flush()


@_locked
def inject_key(name: str, down: bool, us: Optional[str] = None) -> None:
    def go(devices):
        group, locks, layout = 0, 0, _layout
        if len(name) == 1:
            layout, group = _typing()
            if layout is None:
                return
            locks = devices.locks()
            if down and name not in _state.chars_down and _beyond(name, us, layout, group, locks):
                # A key of its own each time: a repeat is the peer's next keydown, and the release
                # that follows finds nothing held.
                held = _held_codes(_state.mods_down)
                _press(devices, [(code, False) for code in held])
                try:
                    _beyond_shift(devices, name, layout, group, locks)
                finally:
                    _press(devices, [(code, True) for code in held])
                return
        steps = plan_key(name, down, us, _state, layout, group, locks)
        if steps:
            _press(devices, wayland_steps(steps))

    _play(go)


def _beyond(character: str, us: Optional[str], layout, group: int, locks: int) -> bool:
    """Whether `character` is typed by the layout's other levels, a dead key or wtype: no key
    types it with Shift alone, and it is neither a shortcut's key nor another script's letter,
    which go by their place on the keyboard."""
    if _state.mods_down & CHORD_KEYS or layout.find(character, group, locks) is not None:
        return False
    return _by_place(character, us, _state.mods_down, lambda key: layout.place(key, group)) is None


@_locked
def inject_text(text: str) -> None:
    def go(devices):
        layout, group = _typing()
        if layout is None:
            return
        locks = devices.locks()
        # Every modifier the peer holds is let go round the whole of it (WIRE.md section 10).
        held = _held_codes(_state.mods_down)
        bare = KeyState()
        bare.spares, bare.chars_down = _state.spares, _state.chars_down
        _press(devices, [(code, False) for code in held])
        try:
            run, keyed = "", True
            for character in text + "\0":
                has_key = character != "\0" and layout.find(character, group, locks) is not None
                if run and (has_key != keyed or character == "\0"):
                    if keyed:
                        _press(devices, wayland_steps(plan_text(run, bare, layout, group, locks)))
                    else:
                        _beyond_shift(devices, run, layout, group, locks)
                    run = ""
                run, keyed = run + character, has_key
        finally:
            _press(devices, [(code, True) for code in held])

    _play(go)


def inject_mouse_move(dx: int, dy: int) -> None:
    dx, dy = int(dx), int(dy)
    if not dx and not dy:
        return

    def go(devices):
        devices.move(dx, dy)
        if not desktop_wlroots.tells_cursor():
            desktop_wlroots.note_motion(dx, dy)

    _play(go)


def _move_to(x: int, y: int) -> None:
    """desktop_wlroots's mover: an absolute placement, where no compositor socket makes one."""
    point = absolute_point(int(x), int(y), desktop_wlroots.monitors())
    _play(lambda devices: devices.move_to(*point))


@_locked
def inject_mouse_button(button: str, down: bool) -> None:
    code = BUTTONS.get(button)
    if code is None:
        _warn_once("Unknown mouse button ignored: %r", button)
        return
    if down:
        _buttons_down.add(button)
    else:
        _buttons_down.discard(button)
    _play(lambda devices: devices.button(code, down))


@_locked
def inject_scroll(dy, dx=0.0, mode: str = "line") -> None:
    events = plan_wheel(dy, dx, mode, _wheel_carry)
    if events:
        _play(lambda devices: devices.wheel(events))


def inject_gesture(name: str, swipe=None) -> None:
    """Linux desktops share no shortcut for a swipe, so none is pressed; a Linux machine does not
    offer the `gestures` capability, and a peer that sends one anyway gets this."""
    _warn_once("Gestures are not yet on Linux; %r dropped", name)


@_locked
def release_all() -> None:
    """Let go of every key and button held for the peer, the twin of input_injector.release_all:
    the receiver calls it when the peer's input goes home or its link dies."""
    codes, buttons = sorted(_pressed), sorted(_buttons_down)

    def go(devices):
        for code in codes:
            devices.key(code, False)
        for button in buttons:
            devices.button(BUTTONS[button], False)

    if _devices is not None and (codes or buttons):
        _play(go)
    _forget_held()
