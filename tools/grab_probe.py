#!/usr/bin/env python3
"""Try Beamer's evdev capture by hand, one step at a time, before the app is trusted with the
keyboard. Meant to be run over SSH from another device, so that a keyboard that does not come
back is no more than an inconvenience: from that other device, `pkill -9 -f grab_probe` gives it
back at once, since a grab ends with the process that holds it.

    python tools/grab_probe.py list              what is in /dev/input and what this user may open
    python tools/grab_probe.py listen [SECONDS]  read without grabbing; the desktop keeps working
    python tools/grab_probe.py devices [SECONDS] read without grabbing, and say for each device
                                                 (its name and eventN) how many events of each
                                                 kind libinput made of it
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
import select
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


def kind_of(event: tuple) -> str:
    """What an event of libinput.py's is counted as. Never which key or button: only that one was."""
    kind = event[0]
    if kind in ("key", "button"):
        return f"{kind} {'down' if event[3] else 'up'}"
    if kind == "other":
        import libinput

        return f"{libinput.EVENT_NAMES.get(event[2], f'libinput event {event[2]}')} (not sent on)"
    return kind


def tally(events, counts: dict) -> dict:
    """Add `events` to `counts`, a Counter of kinds for each device."""
    for event in events:
        if event[0] not in ("added", "removed"):
            counts.setdefault(event[1], collections.Counter())[kind_of(event)] += 1
    return counts


def report(counts: dict, paths: dict, names: dict) -> list:
    """One block of lines for each device read, in eventN order, the silent ones too."""
    lines = []
    for device, path in sorted(paths.items(), key=lambda item: int(item[1].rsplit("event", 1)[1])):
        lines.append(f"{Path(path).name:8} {names.get(device) or '(no name)'}")
        kinds = counts.get(device)
        if not kinds:
            lines.append("            nothing")
        for kind, count in sorted((kinds or {}).items()):
            lines.append(f"    {count:6}  {kind}")
    return lines


def by_device(seconds: float, system=None, context=None, clock=time.monotonic) -> int:
    system = system or capture.System()
    tap, natural = capture.touchpad_options()

    def opener(path, flags):
        try:
            return system.open(path, flags)
        except PermissionError:  # read only, as the capture itself falls back
            return system.open(path, (flags & ~os.O_ACCMODE) | os.O_RDONLY)

    context = (context or capture._libinput_context)(opener, system.close, tap, natural)
    paths, names = {}, {}
    try:
        for path in system.paths():
            name = system.name(path)
            if name.startswith(capture.OWN_DEVICES):
                continue
            device = context.add(path)
            if device is not None:
                paths[device], names[device] = path, name
        if not paths:
            print("No device could be read. `list` says what this user may open.")
            return 1
        print(f"Reading {len(paths)} devices for {seconds:g} s without grabbing: type, move, click, scroll.", flush=True)
        counts = {}
        deadline = clock() + seconds
        while True:
            left = deadline - clock()
            if left <= 0:
                break
            select.select([context.fileno()], [], [], min(left, 0.1))
            tally(context.events(), counts)
    finally:
        context.close()
    print("Done.")
    print("\n".join(report(counts, paths, names)))
    return 0


def run(seconds: float, grab: bool, hang: bool = False, freeze: bool = False, hooks_class=None) -> int:
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

    hooks = (hooks_class or capture.Hooks)(on_key, on_mouse, on_motion,
                          environ=dict(os.environ, BEAMER_GRAB_MAX_SECONDS=str(seconds)) if grab else os.environ)
    hooks.grab_while(away.is_set)
    # Every move, whatever the compositor: the app itself takes moves at home only where the
    # compositor says where the pointer is, and a shell over SSH has no compositor to ask.
    hooks.home_moves = lambda: True
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
    if not grab and counts.get("motion"):
        print(f"(moves at home are added up and counted {round(1 / capture.HOME_MOTION_SECONDS)} times a second at most; "
              "`devices` counts each one)")
    return 0


def main(arguments) -> int:
    command = arguments[0] if arguments else ""
    seconds = min(float(arguments[1]), MAX_SECONDS) if len(arguments) > 1 else 5.0
    if command == "list":
        return listing()
    if command == "listen":
        return run(seconds, grab=False)
    if command == "devices":
        return by_device(seconds)
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
