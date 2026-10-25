"""Screens the agent sees and acts on: a real X display, or a fake for tests."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Protocol

from PIL import Image

from .actions import Action, Click, Drag, Key, MouseMove, Scroll, TypeText, Wait

_BUTTONS = {"left": "1", "middle": "2", "right": "3"}
# Spire 2 (Godot) only registers a click on a target the pointer is already hovering;
# a click that arrives together with the move is ignored. Pause after moving.
HOVER_SECONDS = "0.25"
_SCROLL_BUTTONS = {"up": "4", "down": "5", "left": "6", "right": "7"}


class Screen(Protocol):
    size: tuple[int, int]

    def capture(self) -> Image.Image: ...

    def perform(self, action: Action) -> None: ...


def xdotool_args(action: Action) -> list[str] | None:
    """The xdotool arguments for an action, or None if it needs no input (wait)."""
    if isinstance(action, Click):
        args = ["mousemove", "--sync", str(action.x), str(action.y), "sleep", HOVER_SECONDS]
        mods = action.modifiers.split("+") if action.modifiers else []
        for mod in mods:
            args += ["keydown", mod]
        args += ["click", "--repeat", str(action.count), _BUTTONS[action.button]]
        for mod in reversed(mods):
            args += ["keyup", mod]
        return args
    if isinstance(action, Drag):
        return [
            "mousemove", "--sync", str(action.x0), str(action.y0), "sleep", HOVER_SECONDS, "mousedown", "1",
            "mousemove", "--sync", str(action.x1), str(action.y1), "sleep", HOVER_SECONDS, "mouseup", "1",
        ]
    if isinstance(action, MouseMove):
        return ["mousemove", "--sync", str(action.x), str(action.y)]
    if isinstance(action, Scroll):
        args = []
        if action.x is not None and action.y is not None:
            args += ["mousemove", "--sync", str(action.x), str(action.y)]
        return args + ["click", "--repeat", str(action.amount), _SCROLL_BUTTONS[action.direction]]
    if isinstance(action, Key):
        return ["key", "--repeat", str(action.repeat), action.keys]
    if isinstance(action, TypeText):
        return ["type", "--delay", "12", "--", action.text]
    if isinstance(action, Wait):
        return None
    raise TypeError(f"unknown action {action!r}")


class X11Screen:
    """An X display (for example Xvfb), captured with mss and driven with xdotool."""

    def __init__(self, display: str = ":99"):
        import mss

        self.display = display
        self._env = {**os.environ, "DISPLAY": display}
        self._mss = mss.MSS(display=display)
        monitor = self._mss.monitors[0]
        self.size = (monitor["width"], monitor["height"])

    def capture(self) -> Image.Image:
        shot = self._mss.grab(self._mss.monitors[0])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def perform(self, action: Action) -> None:
        args = xdotool_args(action)
        if args is None:
            time.sleep(action.seconds)
            return
        subprocess.run(["xdotool", *args], env=self._env, check=True, capture_output=True, timeout=30)

    def pointer(self) -> tuple[int, int]:
        out = subprocess.run(
            ["xdotool", "getmouselocation", "--shell"],
            env=self._env, check=True, capture_output=True, text=True, timeout=10,
        ).stdout
        fields = dict(line.split("=", 1) for line in out.split())
        return int(fields["X"]), int(fields["Y"])


class FakeScreen:
    """Serves fixed frames and records actions instead of performing them."""

    def __init__(self, frames: list[Image.Image]):
        self._frames = frames
        self._index = 0
        self.size = frames[0].size
        self.performed: list[Action] = []

    def capture(self) -> Image.Image:
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame.copy()

    def perform(self, action: Action) -> None:
        self.performed.append(action)
