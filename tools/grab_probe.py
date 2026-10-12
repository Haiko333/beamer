#!/usr/bin/env python3
"""Try Beamer's evdev capture by hand, one step at a time, before the app is trusted with the
keyboard. Meant to be run over SSH from another device, so that a keyboard that does not come
back is no more than an inconvenience: from that other device, `pkill -9 -f grab_probe` gives it
back at once, since a grab ends with the process that holds it.

    python tools/grab_probe.py list              what is in /dev/input and what this user may open
    python tools/grab_probe.py listen [SECONDS]  read without grabbing; the desktop keeps working
    python tools/grab_probe.py grab [SECONDS]    grab every device for SECONDS (5, at most 30)
    python tools/grab_probe.py hang              grab, then hang in a callback at the next key:
                                                 the watchdog must let go within two seconds
    python tools/grab_probe.py freeze            grab, then stop this whole process: the guard
                                                 must kill it within four seconds ("Killed")

Nothing typed is shown or kept: only how many events of each kind arrived, and for a key whether
it is a modifier. `grab`, `hang` and `freeze` start counting three seconds after they are launched, so the
command's own Enter is let go first. During a grab both Shift keys with Esc let go at once."""

import collections
import os
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "win_app"), str(ROOT)]

import capture_libinput as capture  # noqa: E402

MAX_SECONDS = 30.0


def listing() -> int:
    system = capture.System()
    paths = system.paths()
    if not paths:
        print("No /dev/input/event* at all.")
        return 1
    readable = 0
    for path in paths:
        name = system.name(path) or "(no name)"
        if name.startswith(capture.OWN_DEVICES):
            state = "Beamer's own, not read"
        elif os.access(path, os.R_OK | os.W_OK):
            state, readable = "read and write", readable + 1
        elif os.access(path, os.R_OK):
            state, readable = "read only (a key held through a grab cannot be shown as let go)", readable + 1
        else:
            state = "NO ACCESS"
        print(f"{path:24} {state:16} {name}")
    print(f"\n{readable} of {len(paths)} can be opened by this user.")
    if not readable:
        print("Install packaging/linux/70-beamer-input.rules as its first lines say, then run this again.")
    return 0 if readable else 1


def run(seconds: float, grab: bool, hang: bool = False, freeze: bool = False) -> int:
    counts = collections.Counter()
    failures = []
    away = threading.Event()

    def on_key(name, down, code, us):
        kind = "modifier" if code in capture.MODIFIER_CODES else "key"
        counts[f"{kind} {'down' if down else 'up'}"] += 1
        if hang and away.is_set() and down:
            print("Hanging on purpose inside a callback. The watchdog should let go within "
                  f"{capture.STALL_SECONDS:g} s; if the whole process were stuck, the guard would end it after "
                  f"{capture.GUARD_SECONDS:g} s.", flush=True)
            time.sleep(60)
        return True

    def on_mouse(message, x, y, data):
        event = capture.mouse_event(message, data)
        counts[event[0] if event else "other mouse"] += 1
        return True

    def on_motion(dx, dy):
        counts["motion"] += 1

    hooks = capture.Hooks(on_key, on_mouse, on_motion,
                          environ=dict(os.environ, BEAMER_GRAB_MAX_SECONDS=str(seconds)) if grab else os.environ)
    hooks.grab_while(away.is_set)
    hooks.on_failure = lambda error: (failures.append(getattr(error, "sentence", str(error))), away.clear())
    hooks.on_permission = lambda status, detail="", retryable=False: print(f"permission: {status} {detail}".rstrip(), flush=True)
    try:
        hooks.start()
    except capture.CaptureUnavailable as unavailable:
        print(unavailable.sentence)
        return 1
    print(f"Reading {len(hooks._paths)} devices: " + ", ".join(sorted(Path(path).name for path in hooks._paths.values())), flush=True)
    try:
        if grab:
            for left in (3, 2, 1):
                print(f"Grabbing in {left}... (let go of every key)", flush=True)
                time.sleep(1)
            away.set()
            print(f"GRABBED for {seconds:g} s: the desktop no longer gets this keyboard and mouse. "
                  "Both Shift keys with Esc let go at once.", flush=True)
            if freeze:
                time.sleep(1.0)
                print(f"Stopping this whole process now, grab and all. The guard should kill it within "
                      f"{capture.GUARD_SECONDS:g} s and the keyboard come back; the shell will say Killed.", flush=True)
                os.kill(os.getpid(), signal.SIGSTOP)
        else:
            print(f"Listening for {seconds:g} s without grabbing: type, move, click, scroll.", flush=True)
        deadline = time.monotonic() + seconds + (2.0 if grab else 0.0)
        while time.monotonic() < deadline and (not grab or away.is_set() or hooks.grabbed):
            time.sleep(0.1)
        away.clear()
        time.sleep(0.3)
    finally:
        hooks.stop()
    print("Let go." if grab else "Done.")
    for sentence in failures:
        print("Beamer said:", sentence)
    if not counts:
        print("No event arrived.")
    for kind, count in sorted(counts.items()):
        print(f"{count:6}  {kind}")
    return 0


def main(arguments) -> int:
    command = arguments[0] if arguments else ""
    seconds = min(float(arguments[1]), MAX_SECONDS) if len(arguments) > 1 else 5.0
    if command == "list":
        return listing()
    if command == "listen":
        return run(seconds, grab=False)
    if command == "grab":
        return run(seconds, grab=True)
    if command == "hang":
        return run(15.0, grab=True, hang=True)
    if command == "freeze":
        return run(15.0, grab=True, freeze=True)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
