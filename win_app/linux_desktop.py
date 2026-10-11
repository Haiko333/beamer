import logging
import os

LOGGER = logging.getLogger(__name__)

# Compositors built on wlroots that have no IPC Beamer speaks: they get the evdev back ends with
# the shortcut alone, as any desktop without the portals does.
WLROOTS = {"wlroots", "wayfire", "river", "labwc", "dwl", "hikari", "cage"}

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACES = ("InputCapture", "RemoteDesktop", "Clipboard")
# The portal is asked once, as the app starts: a desktop with no portal answers at once, and one
# that never answers is taken as having none.
PROBE_SECONDS = 2.0


def desktop(environ=None) -> str:
    """"kde", "gnome", "hyprland", "sway", "wlroots" (another wlroots compositor) or ""."""
    environ = os.environ if environ is None else environ
    value = environ.get("XDG_CURRENT_DESKTOP") or environ.get("XDG_SESSION_DESKTOP", "")
    names = {name.strip().lower() for name in value.split(":")}
    if names & {"kde", "plasma"}:
        return "kde"
    if "gnome" in names:
        return "gnome"
    # Started from a tty or a bare session file, neither sets XDG_CURRENT_DESKTOP; each exports
    # where its own socket is.
    if "hyprland" in names or environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if "sway" in names or environ.get("SWAYSOCK"):
        return "sway"
    if names & WLROOTS:
        return "wlroots"
    return ""


def portals(timeout: float = PROBE_SECONDS) -> frozenset:
    """The desktop portals this session has, of PORTAL_INTERFACES, by their short names. Empty when
    the portal cannot be reached at all (no session bus, no jeepney, no portal service): a missing
    portal is an answer here, never an error."""
    try:
        from jeepney import DBusAddress, MessageType, new_method_call
        from jeepney.io.blocking import open_dbus_connection

        connection = open_dbus_connection(bus="SESSION")
    except Exception as exc:
        LOGGER.info("No desktop portal to ask: %s", exc)
        return frozenset()
    found = set()
    try:
        address = DBusAddress(PORTAL_PATH, PORTAL_BUS, "org.freedesktop.DBus.Properties")
        for name in PORTAL_INTERFACES:
            message = new_method_call(address, "Get", "ss", (f"org.freedesktop.portal.{name}", "version"))
            reply = connection.send_and_get_reply(message, timeout=timeout)
            if reply.header.message_type != MessageType.error:
                found.add(name)
    except Exception as exc:
        # A portal that stopped answering part way: what it did answer for stands.
        LOGGER.info("The desktop portal did not answer: %s", exc)
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return frozenset(found)
