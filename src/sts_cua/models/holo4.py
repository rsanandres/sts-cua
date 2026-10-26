"""Holo4 adapter (Hcompany/Holo4-27B GGUF on llama-server, a fine-tune of Qwen3.8-27B).

Format, from H Company's own material (read 2026-10-26):
- Model card (huggingface.co/Hcompany/Holo4-27B): built on Qwen3.8-27B, same chat
  template (chat_template.jinja is byte-identical to Qwen3.8-27B's), used through
  function calling with H's harness.
- Models API docs, "Function calling" (hub.hcompany.ai/models-api/build-an-agent/
  function-calling): Holo4 uses "the standard OpenAI tool-calling API"; pass the tools in
  `tools` with tool_choice="required"; a click tool takes `element` (a description of the
  target) and integer `x`, `y` "in [0, 1000]"; the task ends with an `answer` tool whose
  `content` is the report. Don't put tool-format examples in the system prompt.
- "Core concepts" (same site): "Holo returns integer coordinates in [0, 1000], normalized
  to that image, origin top-left", converted as int((x / 1000) * screenshot.width); each
  screenshot is a user message of three chunks, "<observation>\\n", the image, and
  "\\n</observation>". Agent loops: thinking on, temperature 0.8; single calls: thinking
  off, temperature 0.0. For holo4-27b, reasoning_effort low/medium/xhigh (xhigh default),
  "medium suits agent loops".
- Tool names and arguments: H's published Holo4 trajectories (huggingface.co/datasets/
  Hcompany/trajectories, OSWorld runs of Holo4 27B) call click_desktop {element, x, y,
  button}, double_click_desktop, move_to_desktop, scroll_desktop {element, x, y, direction,
  scroll_size}, hotkey_desktop {keys, repeat_count}, write_desktop and write_at_desktop
  {content, (element, x, y,) press_enter, overwrite}, wait_desktop {seconds},
  mouse_down_desktop / mouse_up_desktop {button} and answer {content}. Their system prompt
  begins "You are Holo, a computer-use agent designed by H Company." and says "Pointer tools
  take integer coordinates on a 0-1000 grid over the screenshot"; the parts that apply here
  are quoted in SYSTEM_HEADER.

UNVERIFIED: H doesn't publish the JSON schemas of those desktop tools; TOOLS below are
rebuilt from the argument names in the trajectories (descriptions are ours). Also whether
llama-server honours tool_choice="required" together with thinking for this template;
Holo4Config.tool_choice can be set to "auto". Replies are read from message.tool_calls
(llama-server parses them with --jinja) and, as a fallback, from <tool_call> XML or JSON
left in content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .. import coop as _coop
from ..actions import Click, Drag, Key, MouseMove, Scroll, TypeText, Wait
from .base import Observation
from .claude import SYSTEM_PROMPT as _CLAUDE_PROMPT
from .local import (
    DEFAULT_GOAL, Cursor, Finish, LocalAdapter, LocalConfig, Reply, adapt_prompt as _adapt, join_keys,
    parse_tool_body, scroll_amount, server_reasoning, server_tool_calls, split_think, split_tool_calls,
)

SCALE = 1000


def _pointer_tool(name: str, description: str, extra: dict | None = None, required: tuple = ()) -> dict:
    properties = {
        "element": {"type": "string", "description": "Detailed description of the target UI element"},
        "x": {"type": "integer", "description": "X coordinate as integer in [0, 1000]"},
        "y": {"type": "integer", "description": "Y coordinate as integer in [0, 1000]"},
        **(extra or {}),
    }
    return _tool(name, description, properties, ("element", "x", "y", *required))


def _tool(name: str, description: str, properties: dict, required: tuple = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)},
    }}


_WRITE = {
    "content": {"type": "string", "description": "Text to type"},
    "press_enter": {"type": "boolean", "description": "Press Enter after typing"},
    "overwrite": {"type": "boolean", "description": "Select all existing text first, so the new text replaces it"},
}
_BUTTON = {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default left)"}

TOOLS = [
    _pointer_tool("click_desktop", "Click at (x, y) coordinates", {"button": _BUTTON}),
    _pointer_tool("double_click_desktop", "Double-click at (x, y) coordinates"),
    _pointer_tool("move_to_desktop", "Move the pointer to (x, y) without clicking"),
    _pointer_tool("scroll_desktop", "Scroll at (x, y)", {
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "scroll_size": {"type": "integer", "description": "Number of wheel steps"},
    }, ("direction",)),
    _tool("hotkey_desktop", "Press a key or a key combination", {
        "keys": {"type": "array", "items": {"type": "string"}, "description": 'Keys pressed together, e.g. ["ctrl", "a"]'},
        "repeat_count": {"type": "integer", "description": "Press it this many times (default 1)"},
    }, ("keys",)),
    _tool("write_desktop", "Type text into the focused field", _WRITE, ("content",)),
    _pointer_tool("write_at_desktop", "Click the field at (x, y), then type text into it", _WRITE, ("content",)),
    _tool("wait_desktop", "Wait for the screen to finish loading or animating", {
        "seconds": {"type": "number", "description": "Seconds to wait"},
    }, ("seconds",)),
    _tool("mouse_down_desktop", "Press and hold a mouse button at the pointer", {"button": _BUTTON}),
    _tool("mouse_up_desktop", "Release the held mouse button at the pointer", {"button": _BUTTON}),
    _tool("answer", "Provide a final answer", {
        "content": {"type": "string", "description": "The answer content"},
    }, ("content",)),
]

# Quoted from the system prompt in H's Holo4 trajectories, keeping only what applies to
# one game window driven by GUI tools (no shell, files, plan or compaction here).
SYSTEM_HEADER = """You are Holo, a computer-use agent designed by H Company.

# Workflow

You work in a loop. Each step you emit reasoning, then an optional message, then tool calls. The calls run in sequence, their results come back as your next observation, and the loop repeats until you terminate.

Before the tool calls, write a message of one or two short sentences saying what you see and why you act. Your earlier messages come back to you in the log of recent steps.

## Tool calls

Each step ends with one or more tool calls. A response without one does nothing and wastes the step.
- Follow the advertised tool schemas exactly: strings and numbers as-is, booleans as `true`/`false`, none as `null`, lists and dicts as JSON (`["ctrl", "a"]`, never `ctrl, a`). Omit optional parameters rather than passing empty values.
- You may chain several calls in one step. If one fails, the calls after it are skipped.
- A step with a desktop action returns one screenshot, taken after the whole chain has run. Intermediate states are invisible.
- Chain only GUI actions whose targets are visible on the current screenshot. Stop the chain at the first action whose result you need to see.

## GUI interaction

- Describe each pointer target uniquely in the `element` argument, with its visible text, position and anchors.
- Clicks on a half-rendered UI land on the wrong element. If you only need to wait for something to load, call `wait_desktop`. Never respond without a tool call.

# The game

"""

_PIXEL_SENTENCE = "Coordinates are pixels in the screenshot you were given, origin at the top left."
_PROMPT_EDITS = (
    (_PIXEL_SENTENCE, "Pointer tools take integer coordinates on a 0-1000 grid over the screenshot: "
                      "(0, 0) is the top left corner and (1000, 1000) the bottom right."),
    ("(use the key action; key names are lowercase)", '(use hotkey_desktop with keys, for example ["e"]; key names are lowercase)'),
    ("(about x 640, y 300)", "(about x 500, y 417)"),
    ("don't take any action: reply with the word DONE and one line saying what you see.",
     "call answer with one line saying what you see."),
)


def adapt_prompt(prompt: str, strict: bool = False) -> str:
    return _adapt(prompt, _PROMPT_EDITS, "holo4._PROMPT_EDITS", strict)


SYSTEM_PROMPT = adapt_prompt(_CLAUDE_PROMPT)


@dataclass
class Holo4Config(LocalConfig):
    served_model: str = "holo4-27b"
    tool_choice: str = "required"  # H's docs; "auto" if the server mishandles it


def _bool(value) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


class Holo4Model(LocalAdapter):
    family = "holo4"
    scale = SCALE
    floor = True  # int((x / 1000) * width)
    # Agent loops: temperature 0.8 with thinking (H). top_p/top_k: Qwen3.8's thinking values.
    sampling_defaults = {False: (0.0, 1.0, None), True: (0.8, 0.95, 20)}
    max_tokens_defaults = {False: 1024, True: 8192}
    default_effort = "medium"
    system_prompt = SYSTEM_PROMPT
    config_class = Holo4Config

    def system_text(self) -> str:
        coop = _coop.PROMPT if self.config.coop else ""
        return SYSTEM_HEADER + self.config.system_prompt + coop

    def extra_params(self) -> dict:
        params = {"tools": TOOLS}
        if getattr(self.config, "tool_choice", None):
            params["tool_choice"] = self.config.tool_choice
        return params

    def user_text(self, obs: Observation) -> str:
        lines = self.history_lines(obs)
        progress = ("Recent steps (coordinates on the 0-1000 grid):\n" + "\n".join(lines)) if lines \
            else "Recent steps: this is the first step."
        parts = [f"Task: {self.config.goal or DEFAULT_GOAL}", f"Step {obs.step}.", progress]
        if obs.notes:
            parts.append(f"Notes:\n{obs.notes}")
        return "\n\n".join(parts)

    def user_content(self, obs: Observation, image_url: str) -> list[dict]:
        return [
            {"type": "text", "text": self.user_text(obs) + "\n\n"},
            {"type": "text", "text": "<observation>\n"},
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": "\n</observation>"},
        ]

    def read_reply(self, text: str, message: dict) -> Reply:
        reasoning, text = split_think(text)
        reasoning = server_reasoning(message) or reasoning
        thought, bodies = split_tool_calls(text)
        bodies = server_tool_calls(message) + bodies
        calls, errors = [], []
        for body in bodies:
            try:
                name, args = parse_tool_body(body)
                if not isinstance(name, str) or not name:
                    raise ValueError("tool call has no function name")
                calls.append({"name": name, "arguments": args})
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{exc}: {body[:200]}")
        return Reply(thought, reasoning, calls, errors)

    def _xy(self, args: dict, size: tuple[int, int]) -> tuple[int, int] | None:
        if "x" in args and "y" in args:
            return self.point((args["x"], args["y"]), size)
        return self.point(args.get("coordinate"), size)

    def _write(self, args: dict) -> list | None:
        content = args.get("content", args.get("text"))
        if content is None or isinstance(content, (dict, list)):
            return None
        content = str(content)
        out = [Key("ctrl+a")] if _bool(args.get("overwrite")) else []
        if content:
            out.append(TypeText(content))
        if _bool(args.get("press_enter")):
            out.append(Key("Return"))
        return out or None

    def convert(self, call: dict, screen_size: tuple[int, int], cursor: Cursor) -> list | None:
        name = str(call.get("name", "")).removesuffix("_desktop")
        args = call.get("arguments") or {}
        if name == "answer":
            return [Finish("answer", str(args.get("content", args.get("text", ""))))]
        if name in ("click", "double_click", "triple_click", "right_click"):
            point = self._xy(args, screen_size)
            if point is None:
                return None
            button = "right" if name == "right_click" else str(args.get("button") or "left").lower()
            if button not in ("left", "right", "middle"):
                return None
            count = {"double_click": 2, "triple_click": 3}.get(name, 1)
            return [Click(*point, button=button, count=count)]
        if name == "move_to":
            point = self._xy(args, screen_size)
            return [MouseMove(*point)] if point else None
        if name == "scroll":
            direction = str(args.get("direction", "")).lower()
            if direction not in ("up", "down", "left", "right"):
                return None
            point = self._xy(args, screen_size)
            x, y = point if point else (None, None)
            return [Scroll(direction, scroll_amount(args.get("scroll_size", 3)), x, y)]
        if name == "hotkey":
            keys = join_keys(args.get("keys", args.get("key")))
            if not keys:
                return None
            return [Key(keys, repeat=max(1, int(args.get("repeat_count") or 1)))]
        if name == "write":
            return self._write(args)
        if name == "write_at":
            point = self._xy(args, screen_size)
            typed = self._write(args)
            return [Click(*point), *typed] if point and typed else None
        if name == "wait":
            return [Wait(float(args.get("seconds", 1)))]
        if name == "mouse_down":
            point = self._xy(args, screen_size)
            if point:
                cursor.pointer = point
            if cursor.pointer is None or str(args.get("button") or "left") != "left":
                return None
            cursor.held = cursor.pointer
            return []
        if name == "mouse_up":
            end = self._xy(args, screen_size) or cursor.pointer
            start, cursor.held = cursor.held, None
            return [Drag(*start, *end)] if start and end else None
        return None  # zoom, shell, key_down/up and anything else this harness doesn't offer
