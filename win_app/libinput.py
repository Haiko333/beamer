"""libinput over ctypes, for the evdev capture: the keyboards, mice and touchpads of this machine
read below the compositor, with a touchpad's touches already turned into pointer moves, taps and
scrolls as the compositor's own libinput turns them.

A path context, not a udev one: Beamer names the devices it reads (capture_libinput lists
/dev/input) and opens each itself, so it holds every file descriptor and can grab and let go of a
device on it. libinput asks for a device to be opened through `open_restricted`; what the caller's
`opener` returns is the descriptor libinput reads.

Each event is a plain tuple, as libei.py's are, with the device as an opaque key:

- ("added", device, name) and ("removed", device)
- ("key", device, evdev code, down)
- ("button", device, evdev code, down)
- ("motion", device, dx, dy): accelerated, as the pointer moves on this machine
- ("wheel", device, y, x): a wheel, in 120ths of a click
- ("scroll", device, y, x): two fingers on a touchpad or a button held, in libinput's scroll units
- ("other", device, libinput's number for the event): one Beamer sends nowhere, named by
  EVENT_NAMES; told so that what a device produces can be counted

Scroll is positive down and right, as libinput has it. The library is loaded only when a Context
is made, so this module imports anywhere."""

import ctypes
import ctypes.util
import errno
import logging
import os
from typing import Callable, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

EVENT_DEVICE_ADDED, EVENT_DEVICE_REMOVED = 1, 2
EVENT_KEYBOARD_KEY = 300
EVENT_POINTER_MOTION, EVENT_POINTER_BUTTON = 400, 402
EVENT_SCROLL_WHEEL, EVENT_SCROLL_FINGER, EVENT_SCROLL_CONTINUOUS = 404, 405, 406
AXIS_VERTICAL, AXIS_HORIZONTAL = 0, 1

# The events not sent on, for whoever counts them (tools/grab_probe.py).
EVENT_NAMES = {
    401: "pointer motion absolute", 403: "pointer axis (older scroll)",
    500: "touch down", 501: "touch up", 502: "touch motion", 503: "touch cancel", 504: "touch frame",
    600: "tablet tool axis", 601: "tablet tool proximity", 602: "tablet tool tip", 603: "tablet tool button",
    700: "tablet pad button", 701: "tablet pad ring", 702: "tablet pad strip", 703: "tablet pad key", 704: "tablet pad dial",
    800: "gesture swipe begin", 801: "gesture swipe update", 802: "gesture swipe end",
    803: "gesture pinch begin", 804: "gesture pinch update", 805: "gesture pinch end",
    806: "gesture hold begin", 807: "gesture hold end",
    900: "switch toggle",
}

_OPEN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_CLOSE = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_void_p)


class _Interface(ctypes.Structure):
    _fields_ = [("open_restricted", _OPEN), ("close_restricted", _CLOSE)]


_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    path = ctypes.util.find_library("input") or "libinput.so.10"
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise RuntimeError("libinput is not installed; Beamer reads the keyboard and mouse through it") from exc
    p, u32, dbl, i = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_double, ctypes.c_int
    signatures = {
        "libinput_path_create_context": (p, [ctypes.POINTER(_Interface), p]),
        "libinput_path_add_device": (p, [p, ctypes.c_char_p]),
        "libinput_path_remove_device": (None, [p]),
        "libinput_unref": (p, [p]),
        "libinput_get_fd": (i, [p]),
        "libinput_dispatch": (i, [p]),
        "libinput_get_event": (p, [p]),
        "libinput_event_destroy": (None, [p]),
        "libinput_event_get_type": (i, [p]),
        "libinput_event_get_device": (p, [p]),
        "libinput_event_get_keyboard_event": (p, [p]),
        "libinput_event_get_pointer_event": (p, [p]),
        "libinput_event_keyboard_get_key": (u32, [p]),
        "libinput_event_keyboard_get_key_state": (i, [p]),
        "libinput_event_pointer_get_dx": (dbl, [p]),
        "libinput_event_pointer_get_dy": (dbl, [p]),
        "libinput_event_pointer_get_button": (u32, [p]),
        "libinput_event_pointer_get_button_state": (i, [p]),
        "libinput_event_pointer_has_axis": (i, [p, i]),
        "libinput_event_pointer_get_scroll_value": (dbl, [p, i]),
        "libinput_event_pointer_get_scroll_value_v120": (dbl, [p, i]),
        "libinput_device_ref": (p, [p]),
        "libinput_device_unref": (p, [p]),
        "libinput_device_get_name": (ctypes.c_char_p, [p]),
        "libinput_device_config_tap_get_finger_count": (i, [p]),
        "libinput_device_config_tap_set_enabled": (i, [p, i]),
        "libinput_device_config_scroll_has_natural_scroll": (i, [p]),
        "libinput_device_config_scroll_set_natural_scroll_enabled": (i, [p, i]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = restype, argtypes
    _lib = lib
    return lib


class Context:
    """A libinput path context over the devices added to it. Used from one thread.

    `opener(path, flags)` opens a device and returns its descriptor, or raises OSError; `closer(fd)`
    closes one. `tap` turns tap-to-click on for touchpads, and `natural` natural scrolling: a
    context of Beamer's own knows nothing of what the compositor was told for its devices."""

    def __init__(self, opener: Callable[[str, int], int], closer: Callable[[int], None] = os.close,
                 tap: bool = True, natural: bool = False) -> None:
        self._lib = _load()
        self._opener, self._closer = opener, closer
        self._tap, self._natural = tap, natural
        self._devices: Dict[int, int] = {}  # libinput's pointer for a device: the reference kept on it
        # Kept on the object: libinput calls these for as long as the context lives.
        self._open_cb, self._close_cb = _OPEN(self._open), _CLOSE(self._close)
        self._interface = _Interface(self._open_cb, self._close_cb)
        self._context = self._lib.libinput_path_create_context(ctypes.byref(self._interface), None)
        if not self._context:
            raise RuntimeError("libinput could not start")

    def _open(self, path: bytes, flags: int, _data) -> int:
        try:
            return self._opener(os.fsdecode(path), flags)
        except OSError as exc:
            return -(exc.errno or errno.EIO)
        except Exception:
            LOGGER.exception("Could not open an input device for libinput")
            return -errno.EIO

    def _close(self, fd: int, _data) -> None:
        try:
            self._closer(fd)
        except Exception:
            LOGGER.exception("Could not close an input device")

    def fileno(self) -> int:
        return self._lib.libinput_get_fd(self._context)

    def add(self, path: str) -> Optional[int]:
        """Start reading the device at `path`: its key for the events to come, or None when
        libinput will not have it (not an input device it knows, or it could not be opened)."""
        device = self._lib.libinput_path_add_device(self._context, os.fsencode(path))
        if not device:
            return None
        self._lib.libinput_device_ref(device)
        self._devices[device] = device
        if self._tap and self._lib.libinput_device_config_tap_get_finger_count(device) > 0:
            self._lib.libinput_device_config_tap_set_enabled(device, 1)
        if self._lib.libinput_device_config_scroll_has_natural_scroll(device):
            self._lib.libinput_device_config_scroll_set_natural_scroll_enabled(device, 1 if self._natural else 0)
        return device

    def remove(self, device: int) -> None:
        if self._devices.pop(device, None) is not None:
            self._lib.libinput_path_remove_device(device)
            self._lib.libinput_device_unref(device)

    def name(self, device: int) -> str:
        raw = self._lib.libinput_device_get_name(device)
        return raw.decode("utf-8", "replace") if raw else ""

    def events(self) -> List[tuple]:
        """Every event that has arrived, in order."""
        lib = self._lib
        lib.libinput_dispatch(self._context)
        found: List[tuple] = []
        while True:
            event = lib.libinput_get_event(self._context)
            if not event:
                return found
            try:
                read = self._read(event)
                if read is not None:
                    found.append(read)
            finally:
                lib.libinput_event_destroy(event)

    def _read(self, event) -> Optional[tuple]:
        lib = self._lib
        kind = lib.libinput_event_get_type(event)
        device = lib.libinput_event_get_device(event)
        if kind == EVENT_DEVICE_ADDED:
            return "added", device, self.name(device)
        if kind == EVENT_DEVICE_REMOVED:
            return "removed", device
        if kind == EVENT_KEYBOARD_KEY:
            key = lib.libinput_event_get_keyboard_event(event)
            return "key", device, lib.libinput_event_keyboard_get_key(key), lib.libinput_event_keyboard_get_key_state(key) == 1
        if kind in (EVENT_POINTER_MOTION, EVENT_POINTER_BUTTON, EVENT_SCROLL_WHEEL, EVENT_SCROLL_FINGER, EVENT_SCROLL_CONTINUOUS):
            pointer = lib.libinput_event_get_pointer_event(event)
            if kind == EVENT_POINTER_MOTION:
                return "motion", device, lib.libinput_event_pointer_get_dx(pointer), lib.libinput_event_pointer_get_dy(pointer)
            if kind == EVENT_POINTER_BUTTON:
                return ("button", device, lib.libinput_event_pointer_get_button(pointer),
                        lib.libinput_event_pointer_get_button_state(pointer) == 1)
            value = (lib.libinput_event_pointer_get_scroll_value_v120 if kind == EVENT_SCROLL_WHEEL
                     else lib.libinput_event_pointer_get_scroll_value)
            y = value(pointer, AXIS_VERTICAL) if lib.libinput_event_pointer_has_axis(pointer, AXIS_VERTICAL) else 0.0
            x = value(pointer, AXIS_HORIZONTAL) if lib.libinput_event_pointer_has_axis(pointer, AXIS_HORIZONTAL) else 0.0
            return ("wheel" if kind == EVENT_SCROLL_WHEEL else "scroll"), device, y, x
        # The older axis event doubles the scroll ones; gestures, touch and tablets are not sent on.
        return "other", device, kind

    def close(self) -> None:
        if self._context:
            for device in list(self._devices):
                self.remove(device)
            self._lib.libinput_unref(self._context)
            self._context = None
