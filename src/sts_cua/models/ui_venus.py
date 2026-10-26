"""UI-Venus-2 adapter (inclusionAI/UI-Venus-2-9B GGUF on llama-server, from Qwen3.5-9B).

Format, from the official repo inclusionAI/UI-Venus @ 1ffbaf6 (2026-09-17):
- models/computer/computer_example.py is the desktop agent. Its SYSTEM_PROMPT (quoted
  below, without the sudo-password sentence) lists Python-call actions: Click(box=(x1, y1)),
  DoubleClick, TripleClick, RightClick, MiddleClick, Hover, Drag(end=..., start=...),
  Swipe(amount=..., axis='vertical'|'horizontal') at the cursor, Type(content=...) where
  each \\n presses Enter, Hotkey(keys=[...], repeat=N), KeyDown/KeyUp, MouseDown/MouseUp,
  Sequence(actions=[...]) of 2 to 32 actions, Wait(), CallUser(content=...) and
  Finished(content=...). The reply is "<think> ... </think>\\n<action> ... </action>".
- Coordinates: "Coordinates range from the top-left corner (0, 0) to the bottom-right
  corner (999, 999)", converted by normalized_point as int(value * width / 999), clamped.
  (The grounding script, models/grounding/ui_venus2_gd.py, uses 0-1000 instead; that is the
  single-click grounding prompt, not the agent one.) The action is parsed with Python's ast,
  never executed; this does the same.
- The task goes in the system prompt ("### User Task"); each user turn is "Current
  Screenshot:\\n" and the image (build_messages).
- Thinking: the model card (huggingface.co/inclusionAI/UI-Venus-2-9B) says "use
  temperature 1.0, enable reasoning, and retain the complete reasoning history" for agent
  tasks, and "disable reasoning and use temperature 0" for grounding. The repo's computer
  example defaults to thinking off, temperature 0.0, top_p 0.7, max_tokens 4096, and sends
  {"chat_template_kwargs": {"enable_thinking": True}} to turn it on.

With thinking off, the prompt still asks for a <think> block: that short text is the
step's thought (the repo keeps it in history too). With thinking on, the template opens
<think> itself, the server returns it as reasoning, and it stays out of the next step's
history (this harness replays a text log, not the full reasoning the card suggests).

The repo's mobile example writes Click(point=(x, y)) (examples/example_output.json); point=
is read as box=.

UNVERIFIED: how Swipe's amount maps to wheel clicks (taken as clicks, capped like the other
adapters), and how long Wait() should last (WAIT_SECONDS).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .. import coop as _coop
from ..actions import Click, Drag, Key, MouseMove, Scroll, TypeText, Wait
from .base import Observation
from .claude import SYSTEM_PROMPT as _CLAUDE_PROMPT
from .local import (
    DEFAULT_GOAL, Cursor, Finish, LocalAdapter, LocalConfig, Reply, adapt_prompt as _adapt, join_keys,
    scroll_amount, server_reasoning, split_think,
)

NORM = 999
WAIT_SECONDS = 2.0

# computer_example.SYSTEM_PROMPT, verbatim except the sudo-password sentence; {user_task} is the goal.
AGENT_PROMPT = r"""**You are a GUI Agent.**
Your role is to analyze the user's task, provide clear and accurate answers to their questions, and execute the task with precise actions on a desktop operating system.

### Available Actions
You may execute one of the following functions. Coordinates range from the top-left corner (0, 0) to the bottom-right corner (999, 999).
- Click(box=(x1, y1)), or Click()
> Perform a left-click at `box`, or at the current cursor position when `box` is omitted.
- DoubleClick(box=(x1, y1)), or DoubleClick()
> Perform a double-click (selects a word in text). Use `box` to move first, or omit it to act at the current cursor position.
- TripleClick(box=(x1, y1)), or TripleClick()
> Perform a triple-click (selects a line or the content of a single-line input). Use `box` to move first, or omit it to act at the current cursor position.
- RightClick(box=(x1, y1)), or RightClick()
> Perform a right-click to open a context menu. Use `box` to move first, or omit it to act at the current cursor position.
- MiddleClick(box=(x1, y1)), or MiddleClick()
> Perform a middle-click (for example, open a link in a new tab). Use `box` to move first, or omit it to act at the current cursor position.
- Hover(box=(x1, y1))
> Move the cursor immediately to the coordinate WITHOUT clicking.
- Drag(end=(x2, y2), start=(x1, y1))
> Drag to `end` using a fixed 0.5-second drag. `start` is optional; omit it to begin from the current cursor position.
- Swipe(amount=-5, axis='vertical')
> Scroll at the current cursor position. `amount` is an integer from -4096 to 4096 and controls magnitude and direction: vertical positive scrolls up and negative scrolls down; horizontal positive scrolls right and negative scrolls left.
- Type(content='')
> Type the provided text into the focused field. Each `\n` presses Enter.
- Hotkey(keys=['ctrl', 'c'], repeat=1)
> Press 1 to 128 listed keys as a keyboard shortcut. Use `repeat=N` from 1 to 128 to press the shortcut N times.
- KeyDown(keys=['shift'])
> Press 1 to 128 listed keys in order and keep them held across later actions and model turns until a matching `KeyUp`.
- KeyUp(keys=['shift'])
> Release 1 to 128 listed keys in order.
- MouseDown(box=(x1, y1)), or MouseDown()
> Optionally move to `box`, then press and hold the left mouse button across later actions and model turns.
- MouseUp(box=(x1, y1)), or MouseUp()
> Optionally move to `box`, then release the left mouse button.
- Sequence(actions=[Click(box=(x1, y1)), Hotkey(keys=['ctrl', 's'])])
> Execute 2 to 32 actions in order as one open-loop model turn. Nested `Sequence` is not allowed, and `CallUser` or `Finished` may appear only as the final action.
- Wait()
> Wait for the current page, animation, or content to finish loading.
- CallUser(content='')
> Request user takeover or report failure when the task cannot be completed or additional information is required.
- Finished(content='')
> Mark the task as completed successfully and optionally report details in `content`.

### Instructions
- Make sure you understand the task goal to avoid wrong actions.
- Prefer one atomic action per turn. Use `Sequence` only when every child action is already known and no intermediate screenshot is needed; its children execute open-loop.
- `KeyDown`, `KeyUp`, `MouseDown`, and `MouseUp` preserve input state across turns. Release held input explicitly when it is no longer needed.
- Any `keys` list may contain at most 128 non-empty key names; each key name is limited to 1,024 characters.
- `Swipe` is the only scrolling action and always scrolls at the current cursor position.
- Make sure you carefully examine the current screenshot. Sometimes the summarized history might not be reliable, over-claiming some effects.
- To submit/search after typing into a field, end the text with a newline — `Type(content='query\n')` — which types the text and presses Enter in one action.
- To replace the existing content of an input field, use `TripleClick` to select it, then `Type` the new content.
- To open a submenu/dropdown, use `Hover` over the parent item to reveal it, then `Click` the desired entry.
- To use a context menu, `RightClick` the target to open it, then `Click` the desired entry.
- To hold a modifier during another action, use `KeyDown`, the target action, and `KeyUp`. Put them in one `Sequence` only when no intermediate screenshot is needed.
- After launching an app, running a command, downloading, or any slow operation, use `Wait()` to let it finish before continuing.
- To press a key or shortcut several times, use `repeat`, e.g. `Hotkey(keys=['down'], repeat=5)` or `Hotkey(keys=['ctrl', 'z'], repeat=3)`, instead of repeating the action.
- Consider exploring the screen by using the `Swipe` action to scroll and reveal additional content.
- Use `Hotkey` for keyboard shortcuts: copy (`ctrl+c`), paste (`ctrl+v`), save (`ctrl+s`), undo (`ctrl+z`), find (`ctrl+f`), etc.
- If the task cannot be completed or additional information is needed, use `CallUser`. Use `Finished` only after successful completion.

### Output Format
<think> your thinking process </think>
<action> the next action </action>

### User Task
{user_task}"""

GUIDE_HEADER = """

### Game Guide
Playing a card takes a key press and a click on the current screenshot: do both in one Sequence, for example Sequence(actions=[Hotkey(keys=['1']), Click(box=(x1, y1))]).

"""

_PIXEL_SENTENCE = "Coordinates are pixels in the screenshot you were given, origin at the top left."
_PROMPT_EDITS = (
    (_PIXEL_SENTENCE, "Coordinates range from the top-left corner (0, 0) to the bottom-right corner (999, 999)."),
    ("(use the key action; key names are lowercase)", "(use Hotkey, for example Hotkey(keys=['e']); key names are lowercase)"),
    ("(about x 640, y 300)", "(about x 500, y 416)"),
    ("don't take any action: reply with the word DONE and one line saying what you see.",
     "use Finished(content='...') with one line saying what you see."),
)


def adapt_prompt(prompt: str, strict: bool = False) -> str:
    return _adapt(prompt, _PROMPT_EDITS, "ui_venus._PROMPT_EDITS", strict)


SYSTEM_PROMPT = adapt_prompt(_CLAUDE_PROMPT)

# Positional arguments, for replies that drop the keywords (the repo rejects them; this reads them).
_POSITIONAL = {
    "Click": ("box",), "DoubleClick": ("box",), "TripleClick": ("box",), "RightClick": ("box",),
    "MiddleClick": ("box",), "Hover": ("box",), "Drag": ("end", "start"), "Swipe": ("amount", "axis"),
    "Type": ("content",), "Hotkey": ("keys", "repeat"), "KeyDown": ("keys",), "KeyUp": ("keys",),
    "MouseDown": ("box",), "MouseUp": ("box",), "Wait": (), "CallUser": ("content",), "Finished": ("content",),
}
_CLICKS = {"Click": ("left", 1), "DoubleClick": ("left", 2), "TripleClick": ("left", 3),
           "RightClick": ("right", 1), "MiddleClick": ("middle", 1)}


def _single(node: ast.AST) -> dict:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        raise ValueError("action must be a direct function call")
    name = node.func.id
    if name == "Sequence":
        raise ValueError("nested Sequence is not allowed")
    names = _POSITIONAL.get(name, ())
    if len(node.args) > len(names):
        raise ValueError(f"{name}: too many positional arguments")
    args = {key: ast.literal_eval(value) for key, value in zip(names, node.args)}
    for keyword in node.keywords:
        if keyword.arg is None:
            raise ValueError("**kwargs are not allowed")
        args[keyword.arg] = ast.literal_eval(keyword.value)
    return {"name": name, "arguments": args}


def parse_action(text: str) -> list[dict]:
    """One <action> body to its calls, Sequence flattened. Literals only; nothing is executed.

    Raises ValueError (SyntaxError included) when it isn't a supported call expression.
    """
    node = ast.parse(text.strip(), mode="eval").body
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Sequence":
        items = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "actions"), None)
        if not isinstance(items, (ast.List, ast.Tuple)):
            raise ValueError("Sequence requires actions=[...]")
        return [_single(item) for item in items.elts]
    return [_single(node)]


_ACTION = re.compile(r"<action>\s*(.*?)\s*(?:</action>|\Z)", re.DOTALL | re.IGNORECASE)


@dataclass
class VenusConfig(LocalConfig):
    served_model: str = "ui-venus-2-9b"


class VenusModel(LocalAdapter):
    family = "ui-venus-2"
    scale = NORM
    floor = True
    # Off: the repo's computer example. On: the card's temperature 1.0; top_p/top_k from Qwen3.5's card.
    sampling_defaults = {False: (0.0, 0.7, None), True: (1.0, 0.95, 20)}
    max_tokens_defaults = {False: 1024, True: 4096}
    system_prompt = SYSTEM_PROMPT
    config_class = VenusConfig

    def system_text(self) -> str:
        coop = _coop.PROMPT if self.config.coop else ""
        return (AGENT_PROMPT.replace("{user_task}", self.config.goal or DEFAULT_GOAL)
                + GUIDE_HEADER + self.config.system_prompt + coop)

    def user_text(self, obs: Observation) -> str:
        lines = self.history_lines(obs)
        progress = (f"Recent steps (coordinates on the 0-{NORM} grid):\n" + "\n".join(lines)) if lines \
            else "Recent steps: this is the first step."
        parts = [f"Step {obs.step}.", progress]
        if obs.notes:
            parts.append(f"Notes:\n{obs.notes}")
        return "\n\n".join(parts)

    def user_content(self, obs: Observation, image_url: str) -> list[dict]:
        return [
            {"type": "text", "text": self.user_text(obs) + "\n\n"},
            {"type": "text", "text": "Current Screenshot:\n"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]

    def read_reply(self, text: str, message: dict) -> Reply:
        think, rest = split_think(text)
        thinking = server_reasoning(message) or think
        # Thinking on: the template's <think> is reasoning. Off: the prompted <think> is the thought.
        thought, reasoning = ("", thinking) if self.config.thinking else (" ".join(thinking.split()), "")
        blocks = _ACTION.findall(rest)
        if not blocks and "<action" not in rest.lower() and rest.strip():
            blocks = [rest.strip()]  # a bare call, as the repo's parse_response accepts
        calls, errors = [], []
        for block in blocks:
            try:
                calls.extend(parse_action(block))
            except (ValueError, SyntaxError, TypeError, RecursionError) as exc:
                errors.append(f"{exc}: {block[:200]}")
        if not calls and not errors and not thought:
            thought = " ".join(rest.split())
        return Reply(thought, reasoning, calls, errors)

    def _box(self, args: dict, size: tuple[int, int], cursor: Cursor, key: str = "box") -> tuple[int, int] | None:
        value = args.get(key, args.get("point") if key == "box" else None)  # the mobile prompt says point=
        if value is not None:
            return self.point(value, size)
        return cursor.pointer

    def convert(self, call: dict, screen_size: tuple[int, int], cursor: Cursor) -> list | None:
        name, args = call["name"], call.get("arguments") or {}
        if name == "Finished":
            return [Finish("finished", str(args.get("content", "")), "Finished")]
        if name == "CallUser":
            return [Finish("call_user", str(args.get("content", "")), "Asked for help")]
        if name in _CLICKS:
            point = self._box(args, screen_size, cursor)
            button, count = _CLICKS[name]
            return [Click(*point, button=button, count=count)] if point else None
        if name == "Hover":
            point = self.point(args.get("box", args.get("point")), screen_size)
            return [MouseMove(*point)] if point else None
        if name == "Drag":
            start = self._box(args, screen_size, cursor, "start")
            end = self.point(args.get("end"), screen_size)
            return [Drag(*start, *end)] if start and end else None
        if name == "Swipe":
            amount = args.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount == 0:
                return None
            axis = args.get("axis", "vertical")
            if axis == "vertical":
                direction = "up" if amount > 0 else "down"
            elif axis == "horizontal":
                direction = "right" if amount > 0 else "left"
            else:
                return None
            return [Scroll(direction, scroll_amount(amount))]  # at the cursor, as Swipe does
        if name == "Type":
            content = args.get("content")
            if not isinstance(content, str) or not content:
                return None
            out = []
            for i, part in enumerate(content.split("\n")):
                if i:
                    out.append(Key("Return"))
                if part:
                    out.append(TypeText(part))
            return out
        if name == "Hotkey":
            keys = join_keys(args.get("keys"))
            repeat = args.get("repeat", 1)
            if not keys or isinstance(repeat, bool) or not isinstance(repeat, int):
                return None
            return [Key(keys, repeat=min(max(repeat, 1), 128))]
        if name == "Wait":
            return [Wait(WAIT_SECONDS)]
        if name == "MouseDown":
            point = self._box(args, screen_size, cursor)
            if point is None:
                return None
            cursor.pointer = cursor.held = point
            return []
        if name == "MouseUp":
            end = self._box(args, screen_size, cursor)
            start, cursor.held = cursor.held, None
            return [Drag(*start, *end)] if start and end else None
        return None  # KeyDown/KeyUp (no held keys in this harness) and unknown names
