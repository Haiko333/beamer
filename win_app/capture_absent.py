"""The capture part of a Wayland desktop Beamer cannot capture on yet: one with no InputCapture
portal to use, before the evdev capture is there. It has the names the shell and app_config read
from every capture back end, and hooks that say why they cannot start, so this machine is driven
but drives nothing, and says so in the Permissions rows rather than failing."""

from typing import Callable, Optional

from capture_win import (  # noqa: F401  (re-exported for the bridge, which reads them from here)
    BUTTON_TITLES,
    MODIFIER_NAMES,
    REDIRECT,
    RETURN,
    TOGGLE,
    Trigger,
    mouse_event,
)
from capture_x11 import (  # noqa: F401  (the same evdev names and titles as X11)
    NOT_TRIGGER_VKS,
    UNRECORDABLE_TRIGGER_VKS,
    VK_TITLES,
    VK_TO_NAME,
    hook_vk,
    input_title,
)

NO_CAPTURE = ("This desktop has no way for Beamer to capture the keyboard and mouse yet, so this machine "
              "cannot drive another one. It can still be driven.")


class CaptureUnavailable(RuntimeError):
    """Capture cannot happen here; `sentence` says so in the app's words."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


class Hooks:
    def __init__(self, on_key, on_mouse, on_motion) -> None:
        self.on_permission: Optional[Callable[..., None]] = None

    def start(self) -> None:
        if self.on_permission is not None:
            self.on_permission("unavailable", NO_CAPTURE, False)
        raise CaptureUnavailable(NO_CAPTURE)

    def stop(self) -> None:
        pass

    def request_again(self) -> None:
        pass
