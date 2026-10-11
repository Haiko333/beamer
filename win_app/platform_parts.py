"""The one switch between the OS parts the shell drives: capture, injection, clipboard, desktop
geometry, autostart, firewall and unlock, which also carry the touchpad injector and GameInputSvc
(input_injector and desktop_win reach them). Chosen once, at import.

Linux on X11 gets the X11 back ends for capture, injection, the clipboard, desktop geometry and
autostart; Linux on Wayland gets one of two whole sets (`backend`): the portal back ends
(InputCapture, RemoteDesktop and its clipboard, over libei) where the desktop has the RemoteDesktop
portal, as GNOME and KDE do, else the evdev back ends, which need no portal and so serve Hyprland,
sway and every other compositor. The sets are not mixed: desktop_portal's pointer is whatever its
two neighbours last told it. All get the same XDG autostart and linux_stand_ins for the firewall and
unlock, and no Windows module beyond the pure pieces capture_x11 and inject_x11 borrow. Every other platform keeps the Windows
modules, including the Mac, where the win_app tests import them on purpose. The callers keep
their old names (`capture_win` and so on) bound to these, so a test that patches
`kvm_bridge_win.firewall_win` still patches the part in use."""

import os
import sys
from types import SimpleNamespace

import linux_desktop

PARTS = ("capture", "desktop", "clipboard", "autostart", "firewall", "unlock", "injector")
WAYLAND_BACKENDS = ("portal", "evdev")


def session(environ) -> str:
    """"x11" or "wayland": which display server a Linux desktop runs. XDG_SESSION_TYPE says when it
    says either; `startx` from a console leaves it "tty", so then an X display with no Wayland one
    is X11. Under Wayland, DISPLAY is XWayland's, which cannot capture or inject for other apps."""
    kind = environ.get("XDG_SESSION_TYPE", "").lower()
    if kind in ("x11", "wayland"):
        return kind
    if environ.get("DISPLAY") and not environ.get("WAYLAND_DISPLAY"):
        return "x11"
    return "wayland"


def backend(environ, portals=None) -> str:
    """Which set of Linux back ends: "x11", "portal" or "evdev". BEAMER_BACKEND names a Wayland set
    outright (evdev on GNOME or KDE lets the shortcut send input away, which no portal allows);
    otherwise the portal set is used wherever the desktop can be driven through it, and a desktop
    with no RemoteDesktop portal, or no portal at all, gets evdev. `portals` answers what the desktop
    has (linux_desktop.portals) and is asked only when nothing else decides."""
    if session(environ) == "x11":
        return "x11"
    forced = environ.get("BEAMER_BACKEND", "").strip().lower()
    if forced in WAYLAND_BACKENDS:
        return forced
    return "portal" if "RemoteDesktop" in (portals or linux_desktop.portals)() else "evdev"


def load(platform: str, environ=os.environ, portals=None) -> SimpleNamespace:
    # Plain imports, not importlib: PyInstaller finds modules by reading import statements.
    if platform.startswith("linux"):
        import linux_stand_ins

        stand_ins = {"firewall": linux_stand_ins.firewall, "unlock": linux_stand_ins.unlock}
        chosen = backend(environ, portals)
        if chosen == "evdev":
            import autostart_x11
            import capture_absent
            import clipboard_wl
            import desktop_wlroots
            import inject_uinput

            return SimpleNamespace(**dict(
                stand_ins, backend=chosen, capture=capture_absent, desktop=desktop_wlroots,
                clipboard=clipboard_wl, autostart=autostart_x11, injector=inject_uinput,
            ))
        if chosen == "portal":
            import autostart_x11
            import capture_portal
            import clipboard_portal
            import desktop_portal
            import inject_portal

            return SimpleNamespace(**dict(
                stand_ins, backend=chosen, capture=capture_portal, desktop=desktop_portal, clipboard=clipboard_portal,
                autostart=autostart_x11, injector=inject_portal,
            ))
        import autostart_x11
        import capture_x11
        import clipboard_x11
        import desktop_x11
        import inject_x11

        return SimpleNamespace(**dict(
            stand_ins, backend=chosen, capture=capture_x11, desktop=desktop_x11, clipboard=clipboard_x11, autostart=autostart_x11,
            injector=inject_x11,
        ))
    import autostart_win
    import capture_win
    import clipboard_win
    import desktop_win
    import firewall_win
    import input_injector
    import unlock_win

    return SimpleNamespace(
        backend="", capture=capture_win, desktop=desktop_win, clipboard=clipboard_win, autostart=autostart_win,
        firewall=firewall_win, unlock=unlock_win, injector=input_injector,
    )


_parts = load(sys.platform)

# "x11", "portal" or "evdev" on Linux, "" elsewhere.
BACKEND = _parts.backend
# Wayland lets no app draw at the screen's edges: the app says so where it applies.
WAYLAND = sys.platform.startswith("linux") and session(os.environ) != "x11"
# The portals show no full-screen app and no button held before the pointer met a barrier, and let
# input leave only across an edge. The evdev back ends are under none of that.
PORTAL = BACKEND == "portal"

capture = _parts.capture
desktop = _parts.desktop
clipboard = _parts.clipboard
autostart = _parts.autostart
firewall = _parts.firewall
unlock = _parts.unlock
injector = _parts.injector
