# Trying Linux with Linux on real hardware

Nothing here has been run yet: Linux with Linux has only been through the automated tests, where
both ends live in one process. This is what to install on a second machine and what to try.
Machine A is the one already set up (Arch, Hyprland, evdev back ends); machine B is the new one.

## Setting up machine B

Both machines must be on the same network, B's firewall must let in TCP 24820 and UDP 24821, and a
virtual machine needs a bridged network card, not NAT: pairing finds the other machine by UDP on
the local network.

### Arch with Hyprland or sway (evdev back ends)

```sh
sudo pacman -S --needed git base-devel wl-clipboard wtype
git clone -b linux-wlroots https://github.com/Haiko333/beamer.git && cd beamer/packaging/arch
makepkg -si
```

The package brings its dependencies (`pyside6`, `python-pynacl`, `python-xlib`, `python-six`,
`python-jeepney`, `python-evdev`, `libinput`, `libxkbcommon`) and the `/dev/uinput` rule, which is
all B needs to be driven. For B to drive A as well, install the `/dev/input` rule as the note
after installing says. Log out and in, then run `beamer`.

### Fedora with GNOME, a live USB included (portal back ends)

Run from source in a virtual environment; no rule is needed, GNOME asks instead.

```sh
sudo dnf install -y git python3-pip libei wl-clipboard
git clone -b linux-wlroots https://github.com/Haiko333/beamer.git && cd beamer
python3 -m venv .venv
grep -v '^evdev' win_app/requirements-linux.txt | .venv/bin/pip install -r /dev/stdin
BEAMER_DEBUG=1 .venv/bin/python win_app/kvm_bridge_win.py
```

`evdev` is left out: it needs a compiler and the portal back ends never load it. The package names
for `dnf` are from memory and have not been checked on a Fedora machine. On a live USB everything
is lost at restart, so pair again each time. To check the wire against upstream instead, install
upstream's Flatpak 1.5.2 as the README says.

### What to check before pairing

On each machine, the log (`~/.config/Beamer/Beamer.log`) names the back ends in its first lines.
On A, `hyprctl devices | grep -i beamer` shows the virtual keyboard and mouse once A has been driven.

## What to try

Pair with the six-digit code, allow each machine to drive the other, and place them on each
other's Crossing page. Then, in each direction that the back ends allow (sway and GNOME send input
out only as their README limits say):

| # | Do | Expect |
|---|---|---|
| 1 | Push the pointer through the edge towards the other machine | It arrives at the same height; nothing typed leaks into the machine left behind |
| 2 | Push back through the opposite edge | The pointer comes back at the edge it left and at the right height. **Watch which edge and which monitor**: the `focus` message is suspected of losing them |
| 3 | The shortcut, there and back | Same as the edge |
| 4 | Both Shift keys with Esc while away (from an evdev machine) | Input is back at once |
| 5 | Type `azertyuiop &é"'(-è_çà)=` | Every character right |
| 6 | Type `@ # { } [ ] \| \ € ~` | Right. **Uncertain towards GNOME, KDE and X11**, which did not get the typing of AltGr characters that the evdev back ends got |
| 7 | Type `ê ë î ï ô û`, `^x`, `^` then space | One character each, `^x`, `^` |
| 8 | Control+C, Control+V, Control+A, Alt+Tab | They act on the driven machine |
| 9 | Hold a letter | It repeats (a character from AltGr or a dead key does not: known) |
| 10 | Click, right click, drag, scroll with two fingers and with a wheel | They work; note the scroll direction |
| 11 | Copy text on one, paste on the other, both ways; then a picture | Both arrive |
| 12 | Unplug the network of the driven machine while input is there | Input comes back to the driving machine by itself within a few seconds |

For whatever fails, send the last 80 lines of both logs and what was on screen.
