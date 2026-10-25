"""Actions the agent can take, always in screen pixel coordinates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import ClassVar, Union


@dataclass(frozen=True)
class Click:
    kind: ClassVar[str] = "click"
    x: int
    y: int
    button: str = "left"  # left, right, middle
    count: int = 1  # 2 for double click, 3 for triple
    modifiers: str | None = None  # e.g. "shift" or "ctrl+shift"


@dataclass(frozen=True)
class Drag:
    kind: ClassVar[str] = "drag"
    x0: int
    y0: int
    x1: int
    y1: int


@dataclass(frozen=True)
class MouseMove:
    kind: ClassVar[str] = "mouse_move"
    x: int
    y: int


@dataclass(frozen=True)
class Scroll:
    kind: ClassVar[str] = "scroll"
    direction: str  # up, down, left, right
    amount: int
    x: int | None = None
    y: int | None = None


@dataclass(frozen=True)
class Key:
    kind: ClassVar[str] = "key"
    keys: str  # xdotool keysym syntax, e.g. "Return" or "ctrl+s"
    repeat: int = 1


@dataclass(frozen=True)
class TypeText:
    kind: ClassVar[str] = "type"
    text: str


@dataclass(frozen=True)
class Wait:
    kind: ClassVar[str] = "wait"
    seconds: float


Action = Union[Click, Drag, MouseMove, Scroll, Key, TypeText, Wait]


def action_to_dict(action: Action) -> dict:
    return {"kind": action.kind, **asdict(action)}


def describe(action: Action) -> str:
    """One-line description used in the history the model sees."""
    if isinstance(action, Click):
        name = {1: "click", 2: "double click", 3: "triple click"}.get(action.count, "click")
        button = "" if action.button == "left" else f" {action.button}"
        mods = f" with {action.modifiers}" if action.modifiers else ""
        return f"{button.strip()} {name} at ({action.x}, {action.y}){mods}".strip()
    if isinstance(action, Drag):
        return f"drag from ({action.x0}, {action.y0}) to ({action.x1}, {action.y1})"
    if isinstance(action, MouseMove):
        return f"move mouse to ({action.x}, {action.y})"
    if isinstance(action, Scroll):
        where = f" at ({action.x}, {action.y})" if action.x is not None else ""
        return f"scroll {action.direction} {action.amount}{where}"
    if isinstance(action, Key):
        times = f" x{action.repeat}" if action.repeat > 1 else ""
        return f"press {action.keys}{times}"
    if isinstance(action, TypeText):
        return f"type {action.text!r}"
    if isinstance(action, Wait):
        return f"wait {action.seconds:g}s"
    raise TypeError(f"unknown action {action!r}")
