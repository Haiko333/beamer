"""Keyboard and mouse capture below the compositor: the twin of capture_x11.py for a Wayland
desktop with no InputCapture portal to use (Hyprland, sway, any compositor), behind the same
`Hooks`, so the bridge drives it unchanged.

A Wayland compositor shows no app the keys typed into another, and the InputCapture portal, where
there is one, lets input leave only across an edge of the screen. So Beamer reads the devices
themselves, as the compositor does: every keyboard, mouse and touchpad in /dev/input, through
libinput (libinput.py), which turns a touchpad's touches into the same moves, taps and scrolls the
desktop makes of them. That takes a permission (a udev rule opens /dev/input to the person at the
seat; see the README) and it means every key is seen here whether input is away or not, which is
what lets the shortcut send input away and not only bring it back.

While input is away the devices are grabbed (EVIOCGRAB): the kernel then hands their events to
Beamer alone, and nothing reaches the desktop. While it is at home nothing is grabbed, and what
Beamer reads the desktop reads too.

Holding the machine's own keyboard is the one dangerous thing Beamer does, so there are three ways
home that do not depend on the part that went wrong:

- the grab is the kernel's and ends with the process: grab_guard.py, a second process, kills
  Beamer if its capture loop stops turning while the devices are grabbed;
- a watchdog thread lets go of the devices when the capture thread stalls (a callback that never
  returns), as capture_x11's ends its connection;
- both Shift keys with Esc let go at once, read on the capture thread before anything else is asked.

And one for trying it out: BEAMER_GRAB_MAX_SECONDS lets go after that long whatever happens.

A modifier held when the grab is taken (the shortcut's own key, most often) is let go of where
the desktop cannot see it. When the grab ends, the desktop is shown that release (the key is
pressed and released again on its own device), or it would think it held until it was next
touched. Only modifiers: a press the desktop took for a new one would be a letter typed or a
click made, where a modifier's is nothing. A key that types and a mouse button are waited for
briefly before the grab instead, and one still held after that is named in the log.

The devices, the guard and libinput are seams, so everything but the kernel's part is tested
without a keyboard. Nothing here is imported until the hooks start, so this module imports anywhere."""

import fcntl
import glob
import logging
import os
import select
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from capture_win import (  # noqa: F401  (re-exported for the bridge, which reads them from here)
    BUTTON_TITLES,
    MODIFIER_NAMES,
    REDIRECT,
    RETURN,
    TOGGLE,
    WHEEL_DELTA,
    WM_MOUSEHWHEEL,
    WM_MOUSEWHEEL,
    Trigger,
    button_of,
    mouse_event,
    wheel_notches,
)
from capture_x11 import (  # noqa: F401  (the same evdev names and titles as X11)
    _BUTTONS,
    KEYCODE_OFFSET,
    LOCK_MASK,
    NOT_TRIGGER_VKS,
    NUM_LOCK_MASK,
    UNRECORDABLE_TRIGGER_VKS,
    VK_TITLES,
    VK_TO_NAME,
    _wheel_data,
    character,
    hook_vk,
    input_title,
)
from core import keytable
import desktop_wlroots
import grab_guard
import linux_layout

LOGGER = logging.getLogger(__name__)

TICK_SECONDS = 0.05
# The capture thread silent for this long while the devices are grabbed: the watchdog lets go.
STALL_SECONDS = 2.0
# And the whole process silent for this long: the guard ends it. Longer than the watchdog's, which
# is the gentler of the two and should get there first.
GUARD_SECONDS = 4.0
# /dev/input is looked at again this often for a device plugged in or taken away.
SCAN_SECONDS = 2.0
# A grab waits this long for a key that types (not a modifier) to be let go, so the desktop is
# not left repeating it; then it is taken anyway.
GRAB_WAIT_SECONDS = 0.5
# Beamer's own tap on a device comes back to it within a turn of the loop; one not read by then
# is not waited for.
SWALLOW_SECONDS = 0.5
# How long what the compositor said of the keyboard layout is gone by.
LAYOUT_SECONDS = 1.0
# libinput's scroll units for one click of a wheel.
SCROLL_PER_CLICK = 15.0

# evdev's button codes, as X numbers them for capture_x11's table.
EVDEV_BUTTONS = {0x110: 1, 0x111: 3, 0x112: 2, 0x113: 8, 0x114: 9}
# The core modifier bits a held key sets.
HELD_MODS = {42: 1, 54: 1, 29: 4, 97: 4, 56: 8, 125: 64, 126: 64}
# Keys the desktop may be left thinking held through a grab: they type nothing while they wait.
MODIFIER_CODES = frozenset(HELD_MODS) | {100}
KEY_ESC, KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_CAPSLOCK, KEY_NUMLOCK = 1, 42, 54, 58, 69
PANIC_CHORD = frozenset({KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_ESC})
# What the kernel lists as held that matters to a grab: a keyboard's keys and a mouse's buttons. A
# touchpad lists a resting finger as held too (BTN_TOUCH, BTN_TOOL_FINGER), which is neither.
_KEYBOARD_CODES, _MOUSE_BUTTONS = range(1, 0x100), range(0x110, 0x118)
EBUSY = 16
LED_NUML, LED_CAPSL = 0, 1

# The kernel's own: linux/input.h.
EV_SYN, EV_KEY = 0, 1
EVIOCGRAB = 0x40044590
_KEY_BYTES, _LED_BYTES = 96, 8  # KEY_MAX and LED_MAX bits, rounded up


def _eviocg(number: int, length: int) -> int:
    return (2 << 30) | (length << 16) | (ord("E") << 8) | number


EVIOCGKEY, EVIOCGLED = _eviocg(0x18, _KEY_BYTES), _eviocg(0x19, _LED_BYTES)
_EVENT = struct.Struct("llHHi")  # struct input_event: a timeval, type, code, value

# The devices Beamer itself makes for a peer's input (inject_uinput): never read, or a key played
# for a peer would be sent straight back to it.
OWN_DEVICES = "Beamer virtual"

NO_ACCESS = ("Beamer may not read this machine's keyboard and mouse, so it cannot drive another machine: "
             "/dev/input is not open to this user. Install Beamer's udev rule for it "
             "(packaging/linux/70-beamer-input.rules in Beamer's source, described in the README), then choose "
             "Ask again.")
NO_DEVICES = "Beamer found no keyboard or mouse to read on this machine, so it cannot drive another one."
MISSING = "Beamer cannot read this machine's keyboard and mouse: {detail}"
NO_GUARD = ("Beamer did not take this machine's keyboard: the guard that would give it back if Beamer hung "
            "could not be started ({detail}).")
GRAB_FAILED = ("Beamer could not take this machine's keyboard and mouse ({detail}), so input stays here: taken in "
               "part, what is typed would go to the other machine and to this one at once.")
STALLED = ("Beamer stopped answering while it held this machine's keyboard and mouse, so it let go of them "
           "and input is back here.")
PANIC = "Both Shift keys and Esc: Beamer let go of this machine's keyboard and mouse, and input is back here."
TOO_LONG = ("Beamer let go of this machine's keyboard and mouse after {seconds:g} seconds, as "
            "BEAMER_GRAB_MAX_SECONDS says, and input is back here.")
ENDED = "This machine's keyboard and mouse are no longer captured: {detail}"


class CaptureUnavailable(RuntimeError):
    """Capture cannot happen, or was ended, here; `sentence` says so in the app's words."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def _bits(raw: bytes) -> Set[int]:
    return {byte * 8 + bit for byte, value in enumerate(raw) for bit in range(8) if value >> bit & 1}


class System:
    """The kernel's side of the devices: what is there, and the grab."""

    def paths(self) -> List[str]:
        return sorted(glob.glob("/dev/input/event*"), key=lambda path: int(path.rsplit("event", 1)[1]))

    def name(self, path: str) -> str:
        try:
            return (Path("/sys/class/input") / Path(path).name / "device" / "name").read_text().strip()
        except OSError:
            return ""

    def readable(self, path: str) -> bool:
        return os.access(path, os.R_OK)

    def open(self, path: str, flags: int) -> int:
        return os.open(path, flags)

    def close(self, fd: int) -> None:
        os.close(fd)

    def grab(self, fd: int, on: bool) -> None:
        fcntl.ioctl(fd, EVIOCGRAB, 1 if on else 0)

    def keys_down(self, fd: int) -> Set[int]:
        """The keys and buttons the kernel has as held on this device now."""
        return _bits(fcntl.ioctl(fd, EVIOCGKEY, bytes(_KEY_BYTES)))

    def leds(self, fd: int) -> Set[int]:
        return _bits(fcntl.ioctl(fd, EVIOCGLED, bytes(_LED_BYTES)))

    def tap(self, fd: int, code: int) -> None:
        """A press and a release of `code` on the device itself, as if its key were touched. The
        kernel passes on only what changes its own note of the key, so a release alone, of a key
        it knows is up, would go nowhere."""
        for value in (1, 0):
            os.write(fd, _EVENT.pack(0, 0, EV_KEY, code, value) + _EVENT.pack(0, 0, EV_SYN, 0, 0))


class Guard:
    """grab_guard.py as a child process, told at each turn whether the devices are grabbed."""

    def __init__(self, seconds: float = GUARD_SECONDS) -> None:
        script = str(Path(grab_guard.__file__).resolve())
        self._process = subprocess.Popen([sys.executable, script, str(seconds)], stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        self._said: Optional[bytes] = None

    def alive(self) -> bool:
        return self._process.poll() is None

    def beat(self, grabbed: bool) -> None:
        """Raises OSError when the guard is gone."""
        word = grab_guard.GRABBED if grabbed else grab_guard.LET_GO
        if grabbed or word != self._said:
            try:
                os.write(self._process.stdin.fileno(), word)
            except ValueError as exc:  # closed already
                raise OSError(str(exc)) from exc
            self._said = word

    def close(self) -> None:
        try:
            self._process.stdin.close()
            self._process.wait(timeout=1.0)
        except Exception:
            self._process.kill()


def _libinput_context(opener, closer, tap: bool, natural: bool):
    import libinput

    return libinput.Context(opener, closer, tap=tap, natural=natural)


def touchpad_options(environ=None) -> Tuple[bool, bool]:
    """(tap to click, natural scrolling) for touchpads, from BEAMER_TOUCHPAD ("natural", "notap",
    or both with a comma): libinput here knows nothing of what the compositor was told. Tap is on
    unless turned off, as most desktops have it."""
    words = {word.strip().lower() for word in (os.environ if environ is None else environ).get("BEAMER_TOUCHPAD", "").split(",")}
    return "notap" not in words, "natural" in words


def max_grab_seconds(environ=None) -> Optional[float]:
    raw = (os.environ if environ is None else environ).get("BEAMER_GRAB_MAX_SECONDS", "").strip()
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


class Hooks:
    """The capture thread. `on_key(name, down, vk, us)`, `on_mouse(message, x, y, data)` and
    `on_motion(dx, dy)` are called on it, as capture_x11's are; `grab_while(predicate)` says when
    input is away, and the devices are grabbed for as long as it answers True."""

    def __init__(self, on_key, on_mouse, on_motion, system=None, context: Optional[Callable] = None,
                 guard: Optional[Callable] = None, keymap: Optional[Callable] = None, clock=time.monotonic,
                 environ=None) -> None:
        self._on_key, self._on_mouse, self._on_motion = on_key, on_mouse, on_motion
        self._system = system or System()
        self._context_factory = context or _libinput_context
        self._guard_factory = guard or Guard
        self._keymap_factory = keymap or linux_layout.keymap
        self._clock = clock
        self._tap, self._natural = touchpad_options(environ)
        self._max_seconds = max_grab_seconds(environ)
        self.on_permission: Optional[Callable[..., None]] = None
        self.on_failure: Optional[Callable[[BaseException], None]] = None
        self._predicate: Optional[Callable[[], bool]] = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._failure: Optional[BaseException] = None
        self._wake_read, self._wake_write = os.pipe()
        os.set_blocking(self._wake_read, False)
        os.set_blocking(self._wake_write, False)
        # One lock for the descriptors and the grab: the watchdog lets go from its own thread.
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._context = None
        self._guard = None
        self._fds: Dict[str, int] = {}             # path: the descriptor libinput reads
        self._opening: Optional[str] = None
        self._paths: Dict[int, str] = {}           # libinput's device: path
        self._ignored: Set[str] = set()            # paths libinput would not have, or Beamer's own
        self._down: Set[Tuple[int, int]] = set()   # (device, code) held, keys and buttons
        self._names_down: Dict[Tuple[int, int], str] = {}
        self._swallow: Dict[Tuple[int, int], Tuple[int, float]] = {}  # taps of Beamer's own still to be read
        self._grabbed = False
        self._grabbed_at = 0.0
        self._wanted_at: Optional[float] = None
        self._held_at_grab: Dict[str, Set[int]] = {}
        self._refused = False        # let go by force: not grabbed again until input has been home
        self._flips: Set[int] = set()  # lock keys pressed during this grab, which no LED shows
        self._keymap = None
        self._names = None
        self._said: Optional[dict] = None
        self._said_at = 0.0
        self._carry = [0.0, 0.0]
        self._scroll_carry = [0.0, 0.0]
        self._next_scan = 0.0
        self._tick_at = self._clock()

    # -- from any thread ---------------------------------------------------------

    @property
    def grabbed(self) -> bool:
        return self._grabbed

    def grab_while(self, predicate: Callable[[], bool]) -> None:
        self._predicate = predicate

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if not self._stopping.is_set():
                return
            # Stopped but still at it (a callback that has not returned): given a moment, and not
            # doubled.
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                raise CaptureUnavailable(ENDED.format(detail="the last capture has not stopped yet"))
        self._stopping.clear()
        self._ready.clear()
        self._failure = None
        self._reset()
        self._thread = threading.Thread(target=self._run, name="Beamer-hooks", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        if self._failure is not None:
            raise self._failure
        if self._watchdog is not None and self._watchdog.is_alive():
            self._watchdog.join(timeout=TICK_SECONDS * 8)  # one on its way out after a stop
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watch, name="Beamer-hooks-watch", daemon=True)
            self._watchdog.start()

    def request_again(self) -> None:
        try:
            self.start()
        except CaptureUnavailable:
            pass  # said in the permission row already

    def stop(self) -> None:
        self._stopping.set()
        self._wake()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        # A thread that would not end still holds the descriptors: the grab is let go from here.
        self._let_go_from_outside()

    def _wake(self) -> None:
        try:
            os.write(self._wake_write, b"x")
        except OSError:
            pass

    def _let_go_from_outside(self) -> bool:
        """End the grab from another thread than the capture's. True when there was one."""
        was = self._grabbed
        self._ungrab_all()
        if was:
            self._refused = True
            self._flips = set()
        return was

    def _watch(self) -> None:
        while not self._stopping.wait(TICK_SECONDS * 4):
            thread = self._thread
            if thread is None or not thread.is_alive():
                return
            if self._grabbed and self._clock() - self._tick_at > STALL_SECONDS and self._let_go_from_outside():
                LOGGER.error("The capture thread stalled while the devices were grabbed; they were let go")
                # Told from a thread of its own: what stalled the capture may hold what the app's
                # answer waits on, and the watchdog must stay free to let go again.
                threading.Thread(target=self._fail, args=(CaptureUnavailable(STALLED),), name="Beamer-hooks-told",
                                 daemon=True).start()

    # -- the thread --------------------------------------------------------------

    def _run(self) -> None:
        try:
            try:
                self._context = self._context_factory(self._open, self._close, self._tap, self._natural)
            except Exception as exc:
                raise CaptureUnavailable(MISSING.format(detail=exc)) from exc
            denied = self._scan()
            if not self._paths:
                raise CaptureUnavailable(NO_ACCESS if denied else NO_DEVICES)
            if denied:
                LOGGER.warning("%s input devices are not open to this user and are not read; what is typed on "
                               "them stays on this machine", denied)
            self._permission("ready")
            self._ready.set()
            poller = select.poll()
            poller.register(self._context.fileno(), select.POLLIN)
            poller.register(self._wake_read, select.POLLIN)
            while not self._stopping.is_set():
                poller.poll(int(TICK_SECONDS * 1000))
                self._tick_at = now = self._clock()
                try:
                    os.read(self._wake_read, 64)
                except OSError:
                    pass
                for event in self._context.events():
                    try:
                        self._handle(event)
                    except Exception:
                        # One key the app could not take is not the end of the capture.
                        LOGGER.exception("An input event could not be passed on")
                self._settle(now)
                if now >= self._next_scan:
                    self._scan()
                if self._guard is not None:
                    self._guard.beat(self._grabbed)
        except BaseException as exc:
            was_ready = self._ready.is_set()
            if isinstance(exc, CaptureUnavailable):
                self._permission("unavailable", exc.sentence, True)
            else:
                LOGGER.exception("The capture of this machine's keyboard and mouse stopped")
                exc = CaptureUnavailable(ENDED.format(detail=exc))
                self._permission("unavailable", exc.sentence, True)
            self._failure = exc
            if was_ready and not self._stopping.is_set():
                self._fail(exc)
        finally:
            self._end()
            self._ready.set()

    def _end(self) -> None:
        self._ungrab_all()
        if self._guard is not None:
            self._guard.close()
            self._guard = None
        if self._context is not None:
            try:
                self._context.close()  # closes every descriptor through _close
            except Exception:
                LOGGER.exception("libinput did not close cleanly")
            self._context = None
        if self._keymap is not None:
            self._keymap.close()
            self._keymap = None

    def _permission(self, status: str, detail: str = "", retryable: bool = False) -> None:
        if self.on_permission is not None:
            try:
                self.on_permission(status, detail, retryable)
            except Exception:
                LOGGER.exception("Could not report the capture's permission state")

    def _fail(self, error: BaseException) -> None:
        if self.on_failure is not None:
            try:
                self.on_failure(error)
            except Exception:
                LOGGER.exception("Could not report that capture ended")

    # -- the devices -------------------------------------------------------------

    def _open(self, path: str, flags: int) -> int:
        try:
            fd = self._system.open(path, flags)
        except PermissionError:
            # Read only: the device is still read and grabbed, but a key held through a grab cannot
            # be shown as let go afterwards.
            fd = self._system.open(path, (flags & ~os.O_ACCMODE) | os.O_RDONLY)
        with self._lock:
            self._fds[path] = fd
            if self._grabbed:
                # Plugged in while input is away: it is taken with the others.
                try:
                    self._system.grab(fd, True)
                except OSError:
                    pass
        return fd

    def _close(self, fd: int) -> None:
        with self._lock:
            for path, known in list(self._fds.items()):
                if known == fd:
                    del self._fds[path]
        self._system.close(fd)

    def _scan(self) -> int:
        """Bring the devices read in step with /dev/input. Returns how many could not be opened
        for want of permission."""
        self._next_scan = self._clock() + SCAN_SECONDS
        present = self._system.paths()
        denied = 0
        for device, path in list(self._paths.items()):
            if path not in present:
                self._forget(device)
        self._ignored &= set(present)
        known = set(self._paths.values())
        for path in present:
            if path in known or path in self._ignored:
                continue
            if self._system.name(path).startswith(OWN_DEVICES):
                self._ignored.add(path)
                continue
            before = len(self._fds)
            device = self._context.add(path)
            if device is None:
                if len(self._fds) == before and not self._system.readable(path):
                    denied += 1  # tried again at the next look: the rule may be installed meanwhile
                else:
                    self._ignored.add(path)  # not a keyboard or pointer libinput knows
                continue
            self._paths[device] = path
        return denied

    def _forget(self, device: int) -> None:
        self._paths.pop(device, None)
        for key in [key for key in self._down if key[0] == device]:
            self._down.discard(key)
        try:
            self._context.remove(device)
        except Exception:
            LOGGER.exception("Could not stop reading a device that went away")

    # -- the grab ----------------------------------------------------------------

    def _away_wanted(self) -> bool:
        if self._predicate is None:
            return False
        try:
            return bool(self._predicate())
        except Exception:
            LOGGER.exception("Could not tell whether input is away")
            return False

    def _settle(self, now: float) -> None:
        """Take or let go of the devices as the predicate says."""
        wanted = self._away_wanted()
        if not wanted:
            self._refused = False
            self._wanted_at = None
            if self._grabbed:
                self._let_go()
            return
        if self._refused:
            return
        if self._grabbed:
            if self._max_seconds is not None and now - self._grabbed_at >= self._max_seconds:
                self._force_home(TOO_LONG.format(seconds=self._max_seconds))
            return
        if self._wanted_at is None:
            self._wanted_at = now
        held = self._held()
        typing = any(code not in MODIFIER_CODES for codes in held.values() for code in codes)
        if typing and now - self._wanted_at < GRAB_WAIT_SECONDS:
            return
        self._grab(held, now)

    def _held(self) -> Dict[str, Set[int]]:
        held = {}
        for path, fd in list(self._fds.items()):
            try:
                codes = {code for code in self._system.keys_down(fd) if code in _KEYBOARD_CODES or code in _MOUSE_BUTTONS}
            except OSError:
                codes = set()
            if codes:
                held[path] = codes
        return held

    def _grab(self, held: Dict[str, Set[int]], now: float) -> None:
        # The guard first, and told before the first device is taken: there is no moment when the
        # keyboard is held and nothing would give it back.
        try:
            if self._guard is None or not self._guard.alive():
                if self._guard is not None:
                    self._guard.close()
                self._guard = self._guard_factory()
            self._guard.beat(True)
        except Exception as exc:
            LOGGER.exception("The grab guard could not be started; the keyboard is not taken")
            self._guard = None
            self._refused = True
            self._fail(CaptureUnavailable(NO_GUARD.format(detail=exc)))
            return
        failed = None
        with self._lock:
            # Said first: whatever happens from here, every way home lets go of every device.
            self._grabbed = True
            self._tick_at = self._clock()
            for path, fd in self._fds.items():
                try:
                    self._system.grab(fd, True)
                except OSError as exc:
                    if exc.errno == EBUSY:
                        # Another program holds it (a remapper, say) and the desktop gets nothing
                        # from it either: its keys reach Beamer through that program's own device.
                        LOGGER.info("%s is held by another program; it is left to it", path)
                        continue
                    failed = f"{path}: {exc}"
                    break
        if failed is not None:
            # All or nothing: a keyboard left with the desktop would type here and there at once.
            self._force_home(GRAB_FAILED.format(detail=failed))
            return
        self._grabbed_at = now
        # Looked at again now that nothing more reaches the desktop: a key pressed while the
        # devices were being taken is one it saw go down.
        for path, codes in self._held().items():
            held.setdefault(path, set()).update(codes)
        self._held_at_grab = held
        self._flips = set()

    def _ungrab_all(self) -> Dict[str, int]:
        """Let go of every device, whatever the flag says, and tell the guard there is nothing
        left to end Beamer for. From any thread; never raises."""
        with self._lock:
            fds = dict(self._fds)
            for fd in fds.values():
                try:
                    self._system.grab(fd, False)
                except Exception:
                    pass
            self._grabbed = False
            guard = self._guard
        if guard is not None:
            try:
                guard.beat(False)
            except Exception:
                pass
        return fds

    def _let_go(self) -> None:
        fds = self._ungrab_all()
        held, self._held_at_grab = self._held_at_grab, {}
        # The lock keys pressed while away changed nothing the desktop knows of.
        self._flips = set()
        self._after_grab(fds, held)

    def _after_grab(self, fds: Dict[str, int], held: Dict[str, Set[int]]) -> None:
        """Show the desktop the release of each key that was held when the grab was taken and let
        go during it. Beamer reads those taps too, and passes over them."""
        devices = {path: device for device, path in self._paths.items()}
        for path, codes in held.items():
            fd = fds.get(path)
            if fd is None:
                continue
            try:
                still = self._system.keys_down(fd)
            except OSError:
                continue
            for code in sorted(codes - still):
                if code not in MODIFIER_CODES:
                    LOGGER.warning("A key or button (%s) held when input left was let go while it was away; the "
                                   "desktop still has it as held until it is touched once", code)
                    continue
                key = (devices.get(path), code)
                # Before the tap: its press may be read back before the write of its release returns.
                self._swallow[key] = (2, self._clock() + SWALLOW_SECONDS)
                try:
                    self._system.tap(fd, code)
                except OSError as exc:
                    self._swallow.pop(key, None)
                    LOGGER.warning("Could not show the desktop that key %s was let go (%s); touch it once", code, exc)

    def _force_home(self, sentence: str) -> None:
        """Let go now and say why; not grabbed again until the predicate has said input is home."""
        self._let_go()
        self._refused = True
        LOGGER.warning("%s", sentence)
        self._fail(CaptureUnavailable(sentence))

    # -- events ------------------------------------------------------------------

    def _handle(self, event: tuple) -> None:
        kind, device = event[0], event[1]
        if kind == "removed":
            self._forget(device)
        elif kind == "key":
            self._key(device, event[2], event[3])
        elif kind == "button":
            self._button(device, event[2], event[3])
        elif kind == "motion":
            # At home the desktop moves its own pointer; the moves are Beamer's only while away.
            if self._grabbed:
                self._motion(event[2], event[3])
        elif kind == "wheel":
            self._wheel(event[3] / 120.0 * WHEEL_DELTA, event[2] / 120.0 * WHEEL_DELTA)
        elif kind == "scroll":
            self._wheel(event[3] / SCROLL_PER_CLICK * WHEEL_DELTA, event[2] / SCROLL_PER_CLICK * WHEEL_DELTA)

    def _swallowed(self, device: int, code: int) -> bool:
        left, until = self._swallow.get((device, code), (0, 0.0))
        if not left:
            return False
        if left == 1 or self._clock() > until:
            del self._swallow[(device, code)]
        else:
            self._swallow[(device, code)] = (left - 1, until)
        # One that never came back is not waited for: the key's next real press is the user's.
        return self._clock() <= until

    def _key(self, device: int, code: int, down: bool) -> None:
        if self._swallowed(device, code):
            return
        (self._down.add if down else self._down.discard)((device, code))
        if self._grabbed:
            if down and PANIC_CHORD <= {held for _device, held in self._down}:
                self._force_home(PANIC)
                return
            if down and code in (KEY_CAPSLOCK, KEY_NUMLOCK):
                self._flips ^= {code}
        key = (device, code)
        name = self._names_down.get(key) if down else self._names_down.pop(key, None)
        if name is None:
            name = VK_TO_NAME.get(code)
        if name is None:
            name = self._character(device, code)
        if name is None:
            return
        if down:
            self._names_down[key] = name
        self._on_key(name, down, code, keytable.EVDEV_US.get(code))

    def _character(self, device: int, code: int) -> Optional[str]:
        keymap, group = self._typing(device)
        if keymap is None:
            return None
        mods = self._locks(device)
        for _device, held in self._down:
            mods |= HELD_MODS.get(held, 0)
        return character(code + KEYCODE_OFFSET, mods, group, keymap)

    def _locks(self, device: int) -> int:
        """Caps Lock and Num Lock as the keyboard's LEDs show them, and as the lock keys pressed
        during this grab have changed them since: the desktop saw none of those."""
        leds: Set[int] = set()
        fd = self._fds.get(self._paths.get(device, ""))
        if fd is not None:
            try:
                leds = self._system.leds(fd)
            except OSError:
                pass
        caps = (LED_CAPSL in leds) != (KEY_CAPSLOCK in self._flips)
        num = (LED_NUML in leds) != (KEY_NUMLOCK in self._flips)
        return (LOCK_MASK if caps else 0) | (NUM_LOCK_MASK if num else 0)

    def _typing(self, device: int):
        """(the layout this keyboard types in, its group typing now), asked of the compositor at
        most once in LAYOUT_SECONDS and compiled again only when it names another."""
        now = self._clock()
        if now - self._said_at >= LAYOUT_SECONDS or self._said_at == 0.0:
            name = self._system.name(self._paths.get(device, ""))
            self._said, self._said_at = desktop_wlroots.keyboard(name), now
            wanted = next(linux_layout.sources(said=self._said))[1]
            if wanted != self._names:
                if self._keymap is not None:
                    self._keymap.close()
                self._keymap, self._names = None, wanted
                try:
                    self._keymap, _used, origin = self._keymap_factory(said=self._said)
                    LOGGER.info("Reading the keyboard in the layout %s, from %s", wanted.layout or "(default)", origin)
                except Exception:
                    LOGGER.exception("Could not read the keyboard's layout; keys with no fixed name are dropped")
        return self._keymap, (self._said or {}).get("group", 0)

    def _button(self, device: int, code: int, down: bool) -> None:
        if self._swallowed(device, code):
            return
        (self._down.add if down else self._down.discard)((device, code))
        fixed = _BUTTONS.get(EVDEV_BUTTONS.get(code))
        if fixed is None:
            return
        press, release, data = fixed
        self._on_mouse(press if down else release, 0, 0, data)

    def _motion(self, dx: float, dy: float) -> None:
        carry = self._carry
        carry[0] += dx
        carry[1] += dy
        whole_x, whole_y = int(carry[0]), int(carry[1])
        carry[0] -= whole_x
        carry[1] -= whole_y
        if whole_x or whole_y:
            self._on_motion(whole_x, whole_y)

    def _wheel(self, units_x: float, units_y: float) -> None:
        """libinput scrolls positive down and right; Windows' wheel is positive up, its horizontal
        wheel positive right."""
        carry = self._scroll_carry
        carry[0] += units_x
        carry[1] -= units_y
        for index, message in ((1, WM_MOUSEWHEEL), (0, WM_MOUSEHWHEEL)):
            whole = int(carry[index])
            carry[index] -= whole
            if whole:
                self._on_mouse(message, 0, 0, _wheel_data(whole))
