"""Qwen3-VL adapter for a local vLLM server (OpenAI-compatible chat completions).

Every step is a fresh request, like the Claude adapter: a system prompt with the tool
definition, then one user turn with the goal, a short text log of recent steps, any notes,
and the current screenshot.

Format, taken from the Qwen3-VL cookbooks (QwenLM/Qwen3-VL @ 96588727, 2026-01-30):
- computer_use.ipynb and utils/agent_function_call.py define the `computer_use` function
  and build the system prompt with qwen-agent's NousFnCallPrompt: a "# Tools" section
  with the function JSON inside <tools></tools>, and replies as
  <tool_call>{"name": ..., "arguments": {...}}</tool_call>. The tool says "The screen's
  resolution is 1000x1000" and the cookbook maps a coordinate back as x / 1000 * width,
  so coordinates are relative on a 0-1000 scale. (mobile_agent.ipynb uses 999x999 and
  divides by 999 instead; the difference is under 0.1%.)
- mobile_agent.ipynb adds the multi-step format used here: a "# Response format" section
  (Thought, Action, then the tool call), and a "Task progress" line listing earlier steps.
- The model card (huggingface.co/Qwen/Qwen3-VL-8B-Instruct-FP8) recommends, for vision
  tasks, top_p 0.8, top_k 20, temperature 0.7 and presence_penalty 1.5. Presence penalty
  is left off here because it penalizes the braces and quotes a JSON tool call repeats.

https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/computer_use.ipynb
https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/mobile_agent.ipynb
https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/utils/agent_function_call.py

The tool definition goes into the system prompt as text, as in the cookbooks, so vLLM
needs no tool-call parser flags and the reply's <tool_call> blocks arrive in `content`.
The request plumbing, coordinate scaling and run-log fields are shared with the other
local families in models/local.py; models/qwen38.py builds on this adapter.
Decision.raw keeps the request (image replaced by a placeholder; the screenshot is saved
next to the step), the raw reply, usage, per-token logprobs and token IDs when vLLM
returns them, so runs/ can be turned into SFT and RL data later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..actions import Action, Click, Drag, Key, MouseMove, Scroll, TypeText, Wait
from .. import coop as _coop
from .base import Observation
from .claude import SYSTEM_PROMPT as _CLAUDE_PROMPT
from .local import (
    DEFAULT_GOAL, MAX_SCROLL, Cursor, Finish, LocalAdapter, LocalConfig, Reply, Transport, adapt_prompt as _adapt,
    describe_scaled, join_keys, parse_tool_body, post_json, scale_point, scroll_amount, server_reasoning,
    server_tool_calls, split_think, split_tool_calls, to_keysym, to_scale,
)

__all__ = [
    "COMPUTER_USE_TOOL", "DEFAULT_GOAL", "MAX_SCROLL", "QwenConfig", "QwenModel", "SCALE", "SYSTEM_PROMPT",
    "Transport", "adapt_prompt", "describe_relative", "parse_call", "post_json", "split_reply", "to_action",
    "to_keysym", "to_pixels", "to_relative",
]

SCALE = 1000  # the model's coordinate range in both directions

# Verbatim from ComputerUse in the cookbook's utils/agent_function_call.py, at 1000x1000.
COMPUTER_USE_TOOL = {
    "type": "function",
    "function": {
        "name": "computer_use",
        "description": f"""Use a mouse and keyboard to interact with a computer, and take screenshots.
* This is an interface to a desktop GUI. You do not have access to a terminal or applications menu. You must click on desktop icons to start applications.
* Some applications may take time to start or process actions, so you may need to wait and take successive screenshots to see the results of your actions. E.g. if you click on Firefox and a window doesn't open, try wait and taking another screenshot.
* The screen's resolution is {SCALE}x{SCALE}.
* Whenever you intend to move the cursor to click on an element like an icon, you should consult a screenshot to determine the coordinates of the element before moving the cursor.
* If you tried clicking on a program or link but it failed to load, even after waiting, try adjusting your cursor position so that the tip of the cursor visually falls on the element that you want to click.
* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges.""",
        "parameters": {
            "properties": {
                "action": {
                    "description": """The action to perform. The available actions are:
* `key`: Performs key down presses on the arguments passed in order, then performs key releases in reverse order.
* `type`: Type a string of text on the keyboard.
* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.
* `left_click`: Click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `left_click_drag`: Click and drag the cursor to a specified (x, y) pixel coordinate on the screen.
* `right_click`: Click the right mouse button at a specified (x, y) pixel coordinate on the screen.
* `middle_click`: Click the middle mouse button at a specified (x, y) pixel coordinate on the screen.
* `double_click`: Double-click the left mouse button at a specified (x, y) pixel coordinate on the screen.
* `triple_click`: Triple-click the left mouse button at a specified (x, y) pixel coordinate on the screen (simulated as double-click since it's the closest action).
* `scroll`: Performs a scroll of the mouse scroll wheel.
* `hscroll`: Performs a horizontal scroll (mapped to regular scroll).
* `wait`: Wait specified seconds for the change to happen.
* `terminate`: Terminate the current task and report its completion status.
* `answer`: Answer a question.""",
                    "enum": [
                        "key", "type", "mouse_move", "left_click", "left_click_drag", "right_click", "middle_click",
                        "double_click", "triple_click", "scroll", "hscroll", "wait", "terminate", "answer",
                    ],
                    "type": "string",
                },
                "keys": {"description": "Required only by `action=key`.", "type": "array"},
                "text": {"description": "Required only by `action=type` and `action=answer`.", "type": "string"},
                "coordinate": {
                    "description": "(x, y): The x (pixels from the left edge) and y (pixels from the top edge) coordinates to move the mouse to.",
                    "type": "array",
                },
                "pixels": {
                    "description": "The amount of scrolling to perform. Positive values scroll up, negative values scroll down. Required only by `action=scroll` and `action=hscroll`.",
                    "type": "number",
                },
                "time": {"description": "The seconds to wait. Required only by `action=wait`.", "type": "number"},
                "status": {
                    "description": "The status of the task. Required only by `action=terminate`.",
                    "type": "string",
                    "enum": ["success", "failure"],
                },
            },
            "required": ["action"],
            "type": "object",
        },
    },
}

# NousFnCallPrompt's tool section, as printed in computer_use.ipynb.
TOOLS_TEXT = (
    "\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
    "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n"
    + json.dumps(COMPUTER_USE_TOOL)
    + "\n</tools>\n\nFor each function call, return a json object with function name and arguments within "
    "<tool_call></tool_call> XML tags:\n<tool_call>\n{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>"
)

# Adapted from mobile_agent.ipynb: several tool calls are allowed, so a card can be
# selected and played in one step, as with Claude.
RESPONSE_FORMAT = """

# Response format

Response format for every step:
1) Thought: one or two short sentences on what you see and why you take the next action.
2) Action: a short imperative describing what to do in the game.
3) One or more <tool_call>...</tool_call> blocks, each containing only the JSON: {"name": "computer_use", "arguments": <args-json-object>}. They run in order.

Rules:
- Output exactly in the order: Thought, Action, <tool_call>.
- Do not output anything else outside those three parts.
- If finishing, use action=terminate in the tool call."""

_PIXEL_SENTENCE = "Coordinates are pixels in the screenshot you were given, origin at the top left."
_RELATIVE_SENTENCE = (
    f"Coordinates are on a 0 to {SCALE} scale in both directions, whatever the screenshot's size: "
    f"(0, 0) is the top left corner and ({SCALE}, {SCALE}) the bottom right."
)
# Edits that turn the Claude Spire prompt into one for this model's conventions.
_PROMPT_EDITS = (
    (_PIXEL_SENTENCE, _RELATIVE_SENTENCE),
    ("(use the key action; key names are lowercase)", '(use the key action with keys, for example ["e"]; key names are lowercase)'),
    ("(about x 640, y 300)", "(about x 500, y 417)"),
    ("don't take any action: reply with the word DONE and one line saying what you see.",
     "call computer_use with action terminate and status success, and say in the Thought what you see."),
)


def adapt_prompt(prompt: str, strict: bool = False) -> str:
    """Rewrite pixel-coordinate and DONE instructions for this model. strict: every edit must apply."""
    return _adapt(prompt, _PROMPT_EDITS, "qwen._PROMPT_EDITS", strict)


SYSTEM_PROMPT = adapt_prompt(_CLAUDE_PROMPT)  # tests check every edit still applies


@dataclass
class QwenConfig(LocalConfig):
    served_model: str = "Qwen/Qwen3-VL-8B-Instruct-FP8"  # vLLM's --served-model-name, or a LoRA adapter's name
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20
    max_tokens: int = 512
    system_prompt: str = SYSTEM_PROMPT  # the tool definition and response format are appended to it
    return_token_ids: bool = True  # vLLM extension; servers without it ignore the field
    timeout: float = 120.0


def to_pixels(value, screen_size: tuple[int, int]) -> tuple[int, int] | None:
    """A 0-1000 (x, y) from the model to screen pixels, clamped to the screen."""
    return scale_point(value, screen_size, SCALE)


def to_relative(x: int, y: int, screen_size: tuple[int, int]) -> tuple[int, int]:
    return to_scale(x, y, screen_size, SCALE)


def describe_relative(action: Action, screen_size: tuple[int, int]) -> str:
    """describe(), with coordinates on the model's 0-1000 scale."""
    return describe_scaled(action, screen_size, SCALE)


def _keys(args: dict) -> str | None:
    keys = args.get("keys")
    if keys is None:
        keys = args.get("text") or args.get("key")
    return join_keys(keys)


_CLICKS = {
    "left_click": ("left", 1),
    "click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}


def _modifiers(text) -> str | None:
    """A click's optional `text` of held modifiers (Qwen-CUA's tool), e.g. "ctrl+shift"."""
    if not isinstance(text, str) or not text.strip():
        return None
    keys = [to_keysym(k) for k in text.split("+") if k.strip()]
    return "+".join(keys) if keys and all(k in ("ctrl", "shift", "alt", "super") for k in keys) else None


def to_action(args: dict, screen_size: tuple[int, int], pointer: tuple[int, int] | None, point=to_pixels) -> Action | None:
    """One computer_use call to an action in screen pixels, or None if it can't run here.

    pointer is where the cursor is before this call; left_click_drag starts from it.
    point maps the model's coordinate to pixels (0-1000 rounded by default).
    """
    name = args.get("action")
    if name in _CLICKS:
        target = point(args.get("coordinate"), screen_size)
        if target is None:
            return None  # a click at the current cursor; the harness always clicks at a point
        button, count = _CLICKS[name]
        return Click(*target, button=button, count=count, modifiers=_modifiers(args.get("text")))
    if name == "mouse_move":
        target = point(args.get("coordinate"), screen_size)
        return MouseMove(*target) if target else None
    if name == "left_click_drag":
        start = point(args["start_coordinate"], screen_size) if "start_coordinate" in args else pointer
        end = point(args.get("coordinate"), screen_size)
        return Drag(*start, *end) if start and end else None
    if name in ("scroll", "hscroll"):
        pixels = float(args.get("pixels", 0))
        if pixels == 0:
            return None
        # The cookbook passes pixels to pyautogui.scroll, which on X11 counts wheel clicks.
        amount = scroll_amount(pixels)
        if name == "scroll":
            direction = "up" if pixels > 0 else "down"
        else:
            direction = "right" if pixels > 0 else "left"
        target = point(args.get("coordinate"), screen_size)
        x, y = target if target else (None, None)
        return Scroll(direction, amount, x, y)
    if name == "key":
        keys = _keys(args)
        return Key(keys) if keys else None
    if name == "type":
        text = args.get("text")
        return TypeText(text) if isinstance(text, str) and text else None
    if name == "wait":
        return Wait(float(args.get("time", args.get("duration", 1))))
    return None


def split_reply(text: str) -> tuple[str, str, list[str]]:
    """(reasoning inside <think>, text before the first tool call, each tool call's body)."""
    reasoning, text = split_think(text)
    before, calls = split_tool_calls(text)
    return reasoning, before, calls


def parse_call(body: str) -> dict:
    """The arguments of one <tool_call> body (JSON or Qwen3.5+ XML). Raises ValueError when it isn't a computer_use call."""
    name, args = parse_tool_body(body)
    if (name or "computer_use") != "computer_use":
        raise ValueError(f"unknown function {name!r}")
    if "action" not in args:
        raise ValueError("arguments have no action")
    return args


class QwenModel(LocalAdapter):
    family = "qwen3-vl"
    scale = SCALE
    sampling_defaults = {False: (0.7, 0.8, 20)}  # model card; QwenConfig sets them explicitly
    system_prompt = SYSTEM_PROMPT
    config_class = QwenConfig
    tools_text = TOOLS_TEXT
    response_format = RESPONSE_FORMAT

    def __init__(self, config: QwenConfig | None = None, transport: Transport | None = None):
        super().__init__(config, transport)

    def system_text(self) -> str:
        coop = _coop.PROMPT if self.config.coop else ""
        return self.config.system_prompt + coop + self.tools_text + self.response_format

    def user_text(self, obs: Observation) -> str:
        lines = self.history_lines(obs)
        if lines:
            progress = f"Task progress (your recent steps, coordinates on the 0-{SCALE} scale):\n" + "\n".join(lines)
        else:
            progress = "Task progress: this is the first step."
        parts = [f"The user query: {self.config.goal or DEFAULT_GOAL}", f"Step {obs.step}.", progress]
        notes = getattr(obs, "notes", "")
        if notes:
            parts.append(f"Notes:\n{notes}")
        return "\n\n".join(parts)

    def user_content(self, obs: Observation, image_url: str) -> list[dict]:
        return [
            {"type": "text", "text": self.user_text(obs)},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]

    def read_reply(self, text: str, message: dict) -> Reply:
        reasoning, thought, bodies = split_reply(text)
        reasoning = server_reasoning(message) or reasoning
        # A server started with a tool-call parser moves the calls out of content.
        bodies += server_tool_calls(message)
        calls, errors = [], []
        for body in bodies:
            try:
                calls.append(parse_call(body))
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{exc}: {body[:200]}")
        return Reply(thought, reasoning, calls, errors)

    def call_name(self, call) -> str:
        return str(call.get("action"))

    def convert(self, call: dict, screen_size: tuple[int, int], cursor: Cursor) -> list | None:
        name = call.get("action")
        if name == "terminate":
            return [Finish(f"terminate_{call.get('status', 'success')}")]
        if name == "answer":
            return [Finish("answer", str(call.get("text", "")))]
        action = to_action(call, screen_size, cursor.pointer, self.point)
        return [action] if action else None
