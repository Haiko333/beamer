"""The way home that needs nothing of Beamer to run: a second process that ends Beamer when it
stops answering while it holds this machine's keyboard and mouse.

The evdev capture grabs the input devices themselves (capture_libinput). While it holds them no key
reaches the desktop, so a Beamer that hung there would leave the person at the machine with no way
to type, click or switch to a console. The grab is the kernel's and lasts exactly as long as the
process holding it: this guard kills that process, and the keyboard is back.

Beamer starts it and writes a byte to its stdin at every turn of the capture's loop: `g` while the
devices are grabbed, `u` once they are let go. After a `g`, silence for longer than the limit means
Beamer is stuck with the keyboard, and it is killed. Before any `g`, and after a `u`, nothing is
expected and nothing is done. When the pipe closes Beamer has gone by itself and so does the guard.

Run as a script, with the limit in seconds as its argument. It imports nothing of Beamer's."""

import os
import select
import signal
import sys

GRABBED, LET_GO = b"g", b"u"


def watch(limit: float, source: int = 0, parent=None, kill=os.kill) -> str:
    """Returns "gone" when the pipe closed, "killed" when the parent was stuck and was ended."""
    parent = os.getppid() if parent is None else parent
    armed = False
    while True:
        ready, _, _ = select.select([source], [], [], limit if armed else None)
        if not ready:
            # Only its own parent: were Beamer already gone, the pid could be another program's.
            if os.getppid() == parent:
                kill(parent, signal.SIGKILL)
            return "killed"
        data = os.read(source, 4096)
        if not data:
            return "gone"
        armed = data[-1:] == GRABBED


if __name__ == "__main__":
    # The terminal's Ctrl+C and a closing session are for Beamer; the guard outlives them to see
    # it out.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    watch(float(sys.argv[1]) if len(sys.argv) > 1 else 3.0)
