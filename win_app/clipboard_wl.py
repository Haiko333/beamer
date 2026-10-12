"""The clipboard on a Wayland desktop with no portal for it, through wl-clipboard's two tools: the
same functions as clipboard_portal.py, for the evdev back ends.

A Wayland desktop gives the clipboard only to the focused app; a background app reaches it through
the compositor's data-control protocol, which Hyprland, sway, the other wlroots compositors and
KDE have and GNOME does not. wl-copy and wl-paste speak it, so Beamer runs them rather than carry
a Wayland client of its own:

- `wl-paste --watch` runs for as long as Beamer does and has a line printed at each change of the
  clipboard, which is all `change_stamp` needs;
- `wl-paste` reads what another app copied, text and a picture (a PNG as it is, another kind
  turned into one);
- `wl-copy` holds what a peer sent for the apps here to paste.

wl-copy offers one thing at a time, so when a peer sends text and a picture together (a
spreadsheet's cells and their rendering, say) the text is what is set: pasting a picture where
words were copied is the worse of the two losses.

Best effort throughout, as the clipboard is everywhere in Beamer: with the tools missing, or on a
desktop that refuses them, the clipboard reads as empty, nothing is set, the reason is logged
once, and the link and the input it carries are untouched. Every wait here is bounded: the link's
reader calls set_contents and must not stall on a tool that hangs."""

import atexit
import logging
import os
import select
import shutil
import subprocess
import threading
import time
from typing import List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

TEXT_MIMES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")
PNG_MIME = "image/png"
# Pictures Qt reads, turned into PNG for the wire, in the order they are preferred.
OTHER_IMAGES = ("image/jpeg", "image/webp", "image/bmp", "image/x-bmp", "image/tiff", "image/gif")
TIMEOUT_SECONDS = 2.0
# set_contents runs on the link's reader: well under the sender's 1.5 s wait for an
# acknowledgement (core/link.py ACK_STALL_SECONDS).
SET_SECONDS = 0.5
MAX_BYTES = 64 * 1024 * 1024
LIST_BYTES = 64 * 1024
# What password managers put beside a secret they copy, for clipboard tools to leave it alone.
SECRET_HINT = "x-kde-passwordManagerHint"
# The watcher tells of Beamer's own copy as of any other; one that arrives this soon after is that.
ECHO_SECONDS = 2.0
# A watcher that ends is started again after this long, and given up on when it keeps ending at once.
RESTART_SECONDS = 5.0
QUICK_EXIT_SECONDS = 1.0
QUICK_EXITS = 3

_lock = threading.Lock()
_stamp = 0
_ours = False
_echoes: List[float] = []      # when each of Beamer's own copies is due to be told of
_watching = False              # the watcher has started and has not been given up on
_watcher: Optional[threading.Thread] = None
_process = None
_stopping = threading.Event()
_said = set()


def _say_once(key: str, message: str, *args) -> None:
    with _lock:
        if key in _said:
            return
        _said.add(key)
    LOGGER.warning(message, *args)


def _tool(name: str) -> Optional[str]:
    path = shutil.which(name)
    # A PATH with "." or an empty entry in it would name whatever sits in the current directory.
    if path is None or not os.path.isabs(path):
        _say_once(name, "%s is not installed (the wl-clipboard package), so the clipboard is not shared", name)
        return None
    return path


class TooLarge(Exception):
    """A tool printed more than Beamer takes."""


def _run(arguments: List[str], data: Optional[bytes] = None, timeout: float = TIMEOUT_SECONDS,
         limit: int = MAX_BYTES) -> Tuple[int, bytes]:
    """Run a tool to its end within `timeout`: (exit code, what it printed, up to `limit` bytes).
    wl-copy leaves a child behind to hold the clipboard, so nothing it inherits may be a pipe this
    waits on: with `data` to feed it, nothing it prints is read."""
    if data is not None:
        process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        return process.returncode, b""
    process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks, size, deadline = [], 0, time.monotonic() + timeout
    try:
        # Read as it comes, so an app offering a clipboard without end costs `limit`, not memory.
        fd = process.stdout.fileno()
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise TooLarge()
            chunks.append(chunk)
        process.wait(timeout=max(deadline - time.monotonic(), 0.05))
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
    return process.returncode, b"".join(chunks)


# What wl-paste runs at each change. It gets the clipboard on its stdin and never reads it: a line
# on Beamer's pipe is all that is asked. When that pipe is gone, because Beamer was killed and
# could not end its watcher, the line cannot be written and the watcher is ended from here.
WATCH_COMMAND = ("/bin/sh", "-c", 'trap "" PIPE; echo 2>/dev/null || kill "$PPID"')


def _spawn_watcher(path: str):
    return subprocess.Popen([path, "--watch", *WATCH_COMMAND], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)


def _changed() -> None:
    """One line from the watcher: the clipboard changed, by Beamer's own copy or another app's."""
    global _stamp, _ours
    now = time.monotonic()
    with _lock:
        while _echoes and _echoes[0] < now:
            _echoes.pop(0)
        if _echoes:
            _echoes.pop(0)  # set_contents already moved the stamp for this one
            return
        _stamp += 1
        _ours = False


def _watch(path: str) -> None:
    global _process, _watching
    quick = 0
    while not _stopping.is_set():
        started = time.monotonic()
        try:
            process = _spawn_watcher(path)
        except OSError as exc:
            _say_once("watch", "The clipboard cannot be watched (%s), so this machine's copies are not sent back", exc)
            break
        with _lock:
            _process = process
        if _stopping.is_set():
            # stop() came between the look at the flag and the start: it found no process to end.
            process.terminate()
        try:
            # wl-paste tells of the clipboard as it finds it when it starts, which is no change.
            first = True
            for _line in process.stdout:
                if not first:
                    _changed()
                first = False
        finally:
            process.stdout.close()
        process.wait()
        if _stopping.is_set():
            break
        quick = quick + 1 if time.monotonic() - started < QUICK_EXIT_SECONDS else 0
        if quick >= QUICK_EXITS:
            _say_once("watch", "This desktop does not let wl-paste watch the clipboard (exit %s), so this machine's "
                      "copies are not sent back", process.returncode)
            break
        _stopping.wait(RESTART_SECONDS if quick == 0 else 0.2)
    with _lock:
        _watching = False
        _process = None


def _ensure_watching() -> bool:
    global _watcher, _watching
    with _lock:
        if _watching:
            return True
        if _watcher is not None:
            return False  # given up on: this desktop does not allow it
    path = _tool("wl-paste")
    if path is None:
        return False
    with _lock:
        if _watcher is None:
            _watching = True
            _watcher = threading.Thread(target=_watch, args=(path,), name="Beamer-clipboard", daemon=True)
            _watcher.start()
            atexit.register(stop)
        return _watching


def stop() -> None:
    """End the watcher; nothing here starts it again."""
    _stopping.set()
    with _lock:
        process = _process
    if process is not None:
        try:
            process.terminate()
        except OSError:
            pass


def change_stamp() -> Optional[int]:
    """A number that moves whenever the clipboard changes, Beamer's own writes included, or None
    when the clipboard cannot be watched. Read without waiting: the sender asks under its lock."""
    if not _ensure_watching():
        return None
    with _lock:
        return _stamp


def _to_png(data: bytes) -> Optional[bytes]:
    """Another kind of picture as a PNG; QImage is safe off the GUI thread."""
    try:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage

        image = QImage.fromData(data)
        if image.isNull():
            return None
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            return None
        return bytes(buffer.data())
    except Exception:
        LOGGER.exception("Could not turn the copied picture into a PNG")
        return None


def _paste(path: str, mime: str) -> Optional[bytes]:
    try:
        code, out = _run([path, "--no-newline", "--type", mime])
    except TooLarge:
        LOGGER.warning("The clipboard is larger than Beamer sends; leaving it")
        return None
    return out if code == 0 and out else None


def get_contents() -> Tuple[Optional[str], Optional[bytes]]:
    """(text, png) another app has on the clipboard, either None when absent; both None when it
    is Beamer's own, or the clipboard cannot be reached. Never raises."""
    _ensure_watching()
    with _lock:
        if _ours:
            return None, None
    path = _tool("wl-paste")
    if path is None:
        return None, None
    try:
        code, listed = _run([path, "--list-types"], limit=LIST_BYTES)
        if code != 0:
            return None, None  # nothing is copied
        offered = listed.decode("utf-8", "replace").split()
        if SECRET_HINT in offered and _paste(path, SECRET_HINT) == b"secret":
            return None, None  # a password manager's copy stays on this machine
        text = png = None
        mime = next((m for m in TEXT_MIMES if m in offered), None)
        if mime is not None:
            data = _paste(path, mime)
            if data:
                text = data.decode("utf-8", "replace").replace("\r\n", "\n")
        if PNG_MIME in offered:
            png = _paste(path, PNG_MIME)
        else:
            other = next((m for m in OTHER_IMAGES if m in offered), None)
            if other is not None:
                data = _paste(path, other)
                png = _to_png(data) if data else None
        return text or None, png
    except subprocess.TimeoutExpired:
        LOGGER.warning("The app holding the clipboard did not hand it over in time")
        return None, None
    except TooLarge:
        return None, None
    except Exception:
        LOGGER.exception("Could not read the clipboard")
        return None, None


def set_contents(text: Optional[str], png: Optional[bytes]) -> bool:
    """Set `text`, or `png` when there is no text, as this machine's clipboard. False when there is
    nothing to set or the clipboard cannot be reached. Never raises, and never waits longer than
    SET_SECONDS: the link's reader calls this."""
    global _stamp, _ours
    if text is not None:
        mime, data = TEXT_MIMES[0], text.replace("\r\n", "\n").encode("utf-8")
    elif png is not None:
        mime, data = PNG_MIME, png
    else:
        return False
    path = _tool("wl-copy")
    if path is None:
        return False
    watching = _ensure_watching()
    with _lock:
        # Before the copy: the watcher may tell of it before wl-copy returns.
        if watching:
            _echoes.append(time.monotonic() + ECHO_SECONDS)
        was = _ours
        _stamp += 1
        _ours = True
    try:
        code, _ = _run([path, "--type", mime], data, timeout=SET_SECONDS)
    except subprocess.TimeoutExpired:
        # It may have taken the clipboard before it was stopped: the watcher's word of that is
        # still awaited, and the clipboard still counts as Beamer's own, so what a peer sent is
        # never read back as this machine's and sent to it again.
        LOGGER.warning("wl-copy did not take the clipboard in time")
        return False
    except Exception:
        LOGGER.exception("Could not set the clipboard")
        code = -1
    if code != 0:
        with _lock:
            if watching and _echoes:
                _echoes.pop()
            _ours = was  # the stamp stays moved: it only ever says "look again"
        if code > 0:
            _say_once("copy", "This desktop did not let wl-copy set the clipboard (exit %s)", code)
        return False
    return True
