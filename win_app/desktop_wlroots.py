"""Desktop geometry and the pointer for the evdev back ends: the twin of desktop_x11.py, with the
same functions, on a Wayland desktop that has no portal to ask.

Wayland itself tells no app where the pointer is. Hyprland and sway each have their own socket
that does, in part, and this module asks whichever one the session has:

- Hyprland says the displays, where the pointer is, which window is full screen, and moves the
  pointer where it is told;
- sway says the displays and the full-screen window and moves the pointer, but never says where
  the pointer is;
- any other compositor says nothing at all.

So the position is kept here as well, as desktop_portal keeps it: every placement is noted, and the
injector notes each move it plays (`note_motion`). Hyprland's answer replaces the note whenever it
is asked; without one the note is the truth, and it drifts when someone moves this machine's own
mouse while a peer drives it, until the next placement. Where no socket moves the pointer, the
injector's absolute pointer does (`set_mover`), and the displays are what Qt says.

Every function here answers whatever the compositor does: the receiver asks on each move a peer
sends, and a socket that is gone must not end the link. A failure is logged once and the notes
stand in. All positions are in the compositor's logical pixels, the pointer's own space."""

import json
import logging
import os
import socket
import struct
import threading
import time
from typing import Callable, List, Optional, Tuple

from core.return_edge import Rect
from desktop_portal import clamp_to

LOGGER = logging.getLogger(__name__)

# A compositor answers its socket in well under a millisecond; one that takes this long is stuck,
# and the link thread waiting on it must not be.
IPC_SECONDS = 0.25
# The displays are asked on every move a peer sends, and change when a cable does.
MONITORS_SECONDS = 1.0


def hyprland_monitors(reply: list) -> List[Rect]:
    """`j/monitors` as logical rectangles: Hyprland reports each display's pixels and its scale,
    and a display turned on its side keeps its unturned size."""
    found = []
    for monitor in reply:
        if monitor.get("disabled"):
            continue
        scale = float(monitor.get("scale") or 1.0) or 1.0
        width, height = monitor["width"] / scale, monitor["height"] / scale
        if int(monitor.get("transform") or 0) % 2:
            width, height = height, width
        found.append(Rect(int(monitor["x"]), int(monitor["y"]), int(round(width)), int(round(height))))
    return found


def hyprland_full_screen(window: dict) -> Optional[str]:
    """`j/activewindow`: 2 and 3 are full screen, 1 is only maximised; before 0.42 it was a bool."""
    state = window.get("fullscreen") if isinstance(window, dict) else None
    if state is not True and not (isinstance(state, int) and not isinstance(state, bool) and state >= 2):
        return None
    return window.get("class") or window.get("initialClass") or "An app"


def _device_slug(name: str) -> str:
    """A device's name as Hyprland lists it: lower case, with dashes for what is not a letter or digit."""
    return "".join(char if char.isalnum() else "-" for char in name.lower())


def hyprland_keyboard(reply: dict, device: str = "") -> Optional[dict]:
    """`j/devices`: the layout of the keyboard named `device` (Hyprland may give one keyboard its
    own), else of the main one. `layout` and `variant` are XKB's names, comma separated when there
    are several, and `group` is the one typing now."""
    keyboards = [item for item in reply.get("keyboards") or [] if isinstance(item, dict)]
    wanted = _device_slug(device) if device else None
    chosen = next((item for item in keyboards if wanted and item.get("name") == wanted), None) \
        or next((item for item in keyboards if item.get("main")), None) or (keyboards[0] if keyboards else None)
    if chosen is None or not chosen.get("layout"):
        return None
    return {"layout": chosen.get("layout") or "", "variant": chosen.get("variant") or "",
            "options": chosen.get("options") or "", "model": chosen.get("model") or "", "rules": chosen.get("rules") or "",
            "group": int(chosen.get("active_layout_index") or 0)}


def sway_keyboard(reply: list, device: str = "") -> Optional[dict]:
    """`get_inputs`: sway names a layout only as people read it ("French (AZERTY)"), so that is what
    comes back, as `descriptions`, for linux_layout to find XKB's names for."""
    keyboards = [item for item in reply if isinstance(item, dict) and item.get("type") == "keyboard"
                 and item.get("xkb_layout_names")]
    chosen = next((item for item in keyboards if device and item.get("name") == device), None) \
        or (keyboards[0] if keyboards else None)
    if chosen is None:
        return None
    return {"descriptions": list(chosen["xkb_layout_names"]), "group": int(chosen.get("xkb_active_layout_index") or 0)}


def sway_monitors(reply: list) -> List[Rect]:
    """`get_outputs`: sway's rects are already logical."""
    found = []
    for output in reply:
        rect = output.get("rect") or {}
        if output.get("active") and rect.get("width") and rect.get("height"):
            found.append(Rect(int(rect["x"]), int(rect["y"]), int(rect["width"]), int(rect["height"])))
    return found


def sway_full_screen(tree: dict) -> Optional[str]:
    """`get_tree`: the focused container, when it is full screen."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("focused"):
            if not node.get("fullscreen_mode"):
                return None
            return node.get("app_id") or (node.get("window_properties") or {}).get("class") or "An app"
        stack.extend(node.get("nodes") or [])
        stack.extend(node.get("floating_nodes") or [])
    return None


class Hyprland:
    """Hyprland's request socket: one request to a connection, answered and closed."""

    name = "hyprland"
    tells_cursor = True
    # Since Hyprland took its configuration in Lua, a dispatch is a Lua call; before, a word and
    # its arguments.
    MOVE_FORMS = ("dispatch hl.dsp.cursor.move({{x={x}, y={y}}})", "dispatch movecursor {x} {y}")

    def __init__(self, path: str) -> None:
        self.path = path
        self._move_form = self.MOVE_FORMS[0]

    @classmethod
    def find(cls, environ) -> Optional["Hyprland"]:
        signature, runtime = environ.get("HYPRLAND_INSTANCE_SIGNATURE"), environ.get("XDG_RUNTIME_DIR")
        if not signature or not runtime:
            return None
        return cls(os.path.join(runtime, "hypr", signature, ".socket.sock"))

    def request(self, command: str) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as link:
            link.settimeout(IPC_SECONDS)
            link.connect(self.path)
            link.sendall(command.encode("utf-8"))
            chunks = []
            while True:
                chunk = link.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def monitors(self) -> List[Rect]:
        return hyprland_monitors(json.loads(self.request("j/monitors")))

    def cursor(self) -> Tuple[int, int]:
        reply = json.loads(self.request("j/cursorpos"))
        return int(reply["x"]), int(reply["y"])

    def move(self, x: int, y: int) -> None:
        """Tried in the form that last worked: a Hyprland configured in Lua refuses the old words
        and an older one the new, each without moving anything."""
        reply = ""
        for form in (self._move_form, *(form for form in self.MOVE_FORMS if form != self._move_form)):
            reply = self.request(form.format(x=int(x), y=int(y))).strip()
            if reply == "ok":
                self._move_form = form
                return
        raise OSError(f"Hyprland did not move the pointer: {reply[:200]}")

    def full_screen(self) -> Optional[str]:
        return hyprland_full_screen(json.loads(self.request("j/activewindow")))

    def keyboard(self, device: str = "") -> Optional[dict]:
        return hyprland_keyboard(json.loads(self.request("j/devices")), device)

    def plain_pointer(self, name: str, vendor: int, product: int) -> None:
        slug = _device_slug(name)
        reply = ""
        for form in ('eval hl.device({{name="{slug}", accel_profile="flat"}})', "keyword device[{slug}]:accel_profile flat"):
            reply = self.request(form.format(slug=slug)).strip()
            if reply == "ok":
                return
        raise OSError(f"Hyprland did not take the pointer's setting: {reply[:200]}")


class Sway:
    """sway's i3 IPC: a header of the magic, the payload's length and the message type, then JSON."""

    name = "sway"
    tells_cursor = False
    MAGIC = b"i3-ipc"
    RUN_COMMAND, GET_OUTPUTS, GET_TREE, GET_INPUTS = 0, 3, 4, 100

    def __init__(self, path: str) -> None:
        self.path = path

    @classmethod
    def find(cls, environ) -> Optional["Sway"]:
        path = environ.get("SWAYSOCK")
        return cls(path) if path else None

    def request(self, kind: int, payload: str = ""):
        body = payload.encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as link:
            link.settimeout(IPC_SECONDS)
            link.connect(self.path)
            link.sendall(self.MAGIC + struct.pack("<II", len(body), kind) + body)
            header = self._read(link, len(self.MAGIC) + 8)
            if not header.startswith(self.MAGIC):
                raise OSError("sway answered with something that is not i3 IPC")
            length, _kind = struct.unpack("<II", header[len(self.MAGIC):])
            return json.loads(self._read(link, length).decode("utf-8", "replace"))

    @staticmethod
    def _read(link, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = link.recv(count - len(data))
            if not chunk:
                raise OSError("sway closed its socket mid-answer")
            data += chunk
        return data

    def monitors(self) -> List[Rect]:
        return sway_monitors(self.request(self.GET_OUTPUTS))

    def cursor(self) -> Tuple[int, int]:
        raise OSError("sway does not say where the pointer is")

    def move(self, x: int, y: int) -> None:
        reply = self.request(self.RUN_COMMAND, f"seat - cursor set {int(x)} {int(y)}")
        if not (isinstance(reply, list) and reply and all(item.get("success") for item in reply)):
            raise OSError(f"sway did not move the pointer: {reply}")

    def full_screen(self) -> Optional[str]:
        return sway_full_screen(self.request(self.GET_TREE))

    def keyboard(self, device: str = "") -> Optional[dict]:
        return sway_keyboard(self.request(self.GET_INPUTS), device)

    def plain_pointer(self, name: str, vendor: int, product: int) -> None:
        identifier = f"{vendor}:{product}:{name.replace(' ', '_')}"
        reply = self.request(self.RUN_COMMAND, f'input "{identifier}" accel_profile flat')
        if not (isinstance(reply, list) and reply and all(item.get("success") for item in reply)):
            raise OSError(f"sway did not take the pointer's setting: {reply}")


def find_ipc(environ=None):
    """The compositor's own socket for this session, or None when it has none Beamer speaks."""
    environ = os.environ if environ is None else environ
    return Hyprland.find(environ) or Sway.find(environ)


_lock = threading.Lock()
_ipc = None
_ipc_sought = False
_position: Optional[Tuple[float, float]] = None
_mover: Optional[Callable[[int, int], None]] = None
_screens: List[Rect] = []
_monitors: Tuple[float, List[Rect]] = (0.0, [])
_said = set()


def use(ipc) -> None:
    """Set the compositor socket (a test's fake, or None for a desktop with none) and forget what
    the last one said."""
    global _ipc, _ipc_sought, _position, _monitors
    with _lock:
        _ipc, _ipc_sought = ipc, True
        _position, _monitors = None, (0.0, [])
        _said.clear()


def compositor() -> str:
    """"hyprland", "sway" or "": whose socket answers here."""
    ipc = _found()
    return ipc.name if ipc is not None else ""


def tells_cursor() -> bool:
    """Whether the compositor says where the pointer really is. Without that no edge of this screen
    can be watched, and input leaves by the shortcut alone."""
    ipc = _found()
    return ipc is not None and ipc.tells_cursor


def _found():
    global _ipc, _ipc_sought
    with _lock:
        if not _ipc_sought:
            _ipc, _ipc_sought = find_ipc(), True
        return _ipc


def _failed(what: str, exc: BaseException) -> None:
    """Said once for each thing the compositor would not do: these are asked on every move."""
    with _lock:
        if what in _said:
            return
        _said.add(what)
    LOGGER.warning("The compositor did not %s (%s); Beamer goes by its own notes", what, exc)


def set_mover(mover: Optional[Callable[[int, int], None]]) -> None:
    """The injector's absolute move, for a compositor whose socket moves nothing; None when the
    injector's devices are gone."""
    global _mover
    with _lock:
        _mover = mover


def note_motion(dx: float, dy: float) -> None:
    """A relative move the injector just played, for the position kept here."""
    global _position
    displays = monitors()
    with _lock:
        if _position is None:
            first = displays[0]
            _position = (first.x + first.width / 2, first.y + first.height / 2)
        # Held on a display as the real pointer is, though the fraction is not kept past the edge.
        x, y = _position[0] + dx, _position[1] + dy
        held = clamp_to(displays, x, y)
        _position = (x if int(round(x)) == held[0] else held[0], y if int(round(y)) == held[1] else held[1])


def refresh_qt_screens() -> None:
    """Capture Qt geometry on the GUI thread for link and input threads."""
    global _screens
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            return
        screens = [Rect(g.x(), g.y(), g.width(), g.height())
                   for g in (screen.geometry() for screen in app.screens())]
        with _lock:
            _screens = screens
    except Exception:
        LOGGER.exception("Could not read the displays from Qt")


def monitors() -> List[Rect]:
    global _monitors
    now = time.monotonic()
    with _lock:
        at, known = _monitors
        if known and now - at < MONITORS_SECONDS:
            return list(known)
    ipc = _found()
    found: List[Rect] = []
    if ipc is not None:
        try:
            found = ipc.monitors()
        except Exception as exc:
            _failed("list the displays", exc)
    with _lock:
        if found:
            _monitors = (now, found)
            return list(found)
        # A compositor that stopped answering keeps the displays it last named.
        return list(known or _screens) or [Rect(0, 0, 1920, 1080)]


def display_map() -> dict:
    return {str(index): rect for index, rect in enumerate(monitors())}


def cursor_position() -> Tuple[int, int]:
    global _position
    ipc = _found()
    if ipc is not None and ipc.tells_cursor:
        try:
            point = ipc.cursor()
            with _lock:
                _position = (float(point[0]), float(point[1]))
            return point
        except Exception as exc:
            _failed("say where the pointer is", exc)
    with _lock:
        noted = _position
    if noted is not None:
        return int(round(noted[0])), int(round(noted[1]))
    first = monitors()[0]
    return first.x + first.width // 2, first.y + first.height // 2


def set_cursor_position(x: int, y: int) -> None:
    global _position
    x, y = clamp_to(monitors(), x, y)
    with _lock:
        _position = (float(x), float(y))
        mover = _mover
    ipc = _found()
    if ipc is not None:
        try:
            ipc.move(x, y)
            return
        except Exception as exc:
            _failed("move the pointer", exc)
    if mover is not None:
        try:
            mover(x, y)
        except Exception:
            LOGGER.exception("Could not place the pointer")


def button_down(name: str) -> bool:
    return False


def keyboard(device: str = "") -> Optional[dict]:
    """What the compositor says of the keyboard layout (hyprland_keyboard, sway_keyboard), or None
    when it says nothing."""
    ipc = _found()
    if ipc is None:
        return None
    try:
        return ipc.keyboard(device)
    except Exception as exc:
        _failed("say which keyboard layout it has", exc)
        return None


def plain_pointer(name: str, vendor: int, product: int) -> None:
    """Ask the compositor to move the pointer by exactly what the device named `name` reports, with
    no acceleration: a peer's moves are already the distance it means, and the position noted here
    for sway is only right if a move of ten is ten. A compositor with no socket keeps its default."""
    ipc = _found()
    if ipc is None:
        return
    try:
        ipc.plain_pointer(name, vendor, product)
    except Exception as exc:
        _failed("turn off acceleration for Beamer's pointer", exc)


def full_screen_app() -> Optional[str]:
    """The name of the foreground app while it is full screen, else None. GUI thread, once a
    second, as the Mac checks."""
    ipc = _found()
    if ipc is None:
        return None
    try:
        return ipc.full_screen()
    except Exception as exc:
        _failed("say which app is full screen", exc)
        return None
