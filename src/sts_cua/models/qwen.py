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
Decision.raw keeps the request (image replaced by a placeholder; the screenshot is saved
next to the step), the raw reply, usage, per-token logprobs and token IDs when vLLM
returns them, so runs/ can be turned into SFT and RL data later.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from PIL import Image

from ..actions import Action, Click, Drag, Key, MouseMove, Scroll, TypeText, Wait, describe
from .base import Decision, Observation, Usage
from .claude import SYSTEM_PROMPT as _CLAUDE_PROMPT

log = logging.getLogger(__name__)

SCALE = 1000  # the model's coordinate range in both directions
MAX_SCROLL = 10  # wheel clicks per scroll action

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
    for old, new in _PROMPT_EDITS:
        if old in prompt:
            prompt = prompt.replace(old, new)
        elif strict:
            raise ValueError(f"Spire prompt changed; update qwen._PROMPT_EDITS for: {old!r}")
    return prompt


SYSTEM_PROMPT = adapt_prompt(_CLAUDE_PROMPT)  # tests check every edit still applies

DEFAULT_GOAL = "Play Slay the Spire 2 and win the run."


@dataclass
class QwenConfig:
    base_url: str = "http://localhost:8000/v1"
    served_model: str = "Qwen/Qwen3-VL-8B-Instruct-FP8"  # vLLM's --served-model-name, or a LoRA adapter's name
    api_key: str | None = None  # only if vLLM runs with --api-key
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20
    max_tokens: int = 512
    seed: int | None = None
    history_steps: int = 8
    goal: str | None = None
    system_prompt: str = SYSTEM_PROMPT  # the tool definition and response format are appended to it
    logprobs: bool = True
    top_logprobs: int = 0  # alternatives per token, 0 to 20
    return_token_ids: bool = True  # vLLM extension; servers without it ignore the field
    timeout: float = 120.0


# (url, payload, headers, timeout) -> response JSON. Swapped out in tests.
Transport = Callable[[str, dict, dict, float], dict]


def post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"vLLM returned HTTP {exc.code}: {body}") from exc


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def to_pixels(value, screen_size: tuple[int, int]) -> tuple[int, int] | None:
    """A 0-1000 (x, y) from the model to screen pixels, clamped to the screen."""
    if isinstance(value, str):
        value = re.findall(r"-?\d+(?:\.\d+)?", value)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        rx, ry = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    width, height = screen_size
    x = min(max(round(rx / SCALE * width), 0), width - 1)
    y = min(max(round(ry / SCALE * height), 0), height - 1)
    return x, y


def to_relative(x: int, y: int, screen_size: tuple[int, int]) -> tuple[int, int]:
    return round(x / screen_size[0] * SCALE), round(y / screen_size[1] * SCALE)


def describe_relative(action: Action, screen_size: tuple[int, int]) -> str:
    """describe(), with coordinates on the model's 0-1000 scale."""
    if isinstance(action, (Click, MouseMove)) or (isinstance(action, Scroll) and action.x is not None):
        x, y = to_relative(action.x, action.y, screen_size)
        action = dataclasses.replace(action, x=x, y=y)
    elif isinstance(action, Drag):
        x0, y0 = to_relative(action.x0, action.y0, screen_size)
        x1, y1 = to_relative(action.x1, action.y1, screen_size)
        action = Drag(x0, y0, x1, y1)
    return describe(action)


_KEYSYMS = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete", "del": "Delete", "insert": "Insert",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
    "pageup": "Page_Up", "page_up": "Page_Up", "pgup": "Page_Up",
    "pagedown": "Page_Down", "page_down": "Page_Down", "pgdn": "Page_Down",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "cmd": "super", "command": "super", "win": "super", "super": "super", "meta": "super",
}


def to_keysym(name: str) -> str:
    """A key name as the model writes it ("enter", "E", "esc") to an xdotool keysym."""
    name = name.strip()
    lower = name.lower()
    if lower in _KEYSYMS:
        return _KEYSYMS[lower]
    if re.fullmatch(r"f\d{1,2}", lower):
        return lower.upper()
    if len(name) == 1:
        return lower  # "E" would otherwise send shift+e
    return name


def _keys(args: dict) -> str | None:
    keys = args.get("keys")
    if keys is None:
        keys = args.get("text") or args.get("key")
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, list) or not keys:
        return None
    names = [part for key in keys for part in str(key).split("+") if part.strip()]
    return "+".join(to_keysym(n) for n in names) or None


_CLICKS = {
    "left_click": ("left", 1),
    "click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}


def to_action(args: dict, screen_size: tuple[int, int], pointer: tuple[int, int] | None) -> Action | None:
    """One computer_use call to an action in screen pixels, or None if it can't run here.

    pointer is where the cursor is before this call; left_click_drag starts from it.
    """
    name = args.get("action")
    if name in _CLICKS:
        point = to_pixels(args.get("coordinate"), screen_size)
        if point is None:
            return None  # a click at the current cursor; the harness always clicks at a point
        button, count = _CLICKS[name]
        return Click(*point, button=button, count=count)
    if name == "mouse_move":
        point = to_pixels(args.get("coordinate"), screen_size)
        return MouseMove(*point) if point else None
    if name == "left_click_drag":
        start = to_pixels(args["start_coordinate"], screen_size) if "start_coordinate" in args else pointer
        end = to_pixels(args.get("coordinate"), screen_size)
        return Drag(*start, *end) if start and end else None
    if name in ("scroll", "hscroll"):
        pixels = float(args.get("pixels", 0))
        if pixels == 0:
            return None
        # The cookbook passes pixels to pyautogui.scroll, which on X11 counts wheel clicks.
        amount = min(max(round(abs(pixels)), 1), MAX_SCROLL)
        if name == "scroll":
            direction = "up" if pixels > 0 else "down"
        else:
            direction = "right" if pixels > 0 else "left"
        point = to_pixels(args.get("coordinate"), screen_size)
        x, y = point if point else (None, None)
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


def _pointer_after(action: Action) -> tuple[int, int] | None:
    if isinstance(action, (Click, MouseMove)):
        return action.x, action.y
    if isinstance(action, Drag):
        return action.x1, action.y1
    if isinstance(action, Scroll) and action.x is not None:
        return action.x, action.y
    return None


_TOOL_CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.DOTALL)
_THINK = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL)


def split_reply(text: str) -> tuple[str, str, list[str]]:
    """(reasoning inside <think>, text before the first tool call, each tool call's body)."""
    reasoning = " ".join(m.strip() for m in _THINK.findall(text))
    text = _THINK.sub("", text)
    if "</think>" in text:  # Thinking variants open <think> in the prompt, so only the close arrives
        head, text = text.split("</think>", 1)
        reasoning = " ".join(p for p in (head.strip(), reasoning) if p)
    calls = [m.strip() for m in _TOOL_CALL.findall(text)]
    before = text.split("<tool_call>", 1)[0]
    return reasoning, " ".join(before.split()), calls


def parse_call(body: str) -> dict:
    """The arguments of one <tool_call> body. Raises ValueError when it isn't a computer_use call."""
    try:
        call = json.loads(body)
    except json.JSONDecodeError:
        # Trailing junk after the object, e.g. a missing </tool_call> before more text.
        call, _ = json.JSONDecoder().raw_decode(body)
    if not isinstance(call, dict):
        raise ValueError("tool call is not a JSON object")
    if call.get("name", "computer_use") != "computer_use":
        raise ValueError(f"unknown function {call.get('name')!r}")
    args = call.get("arguments", {})
    if isinstance(args, str):
        args = json.loads(args)
    if not isinstance(args, dict) or "action" not in args:
        raise ValueError("arguments have no action")
    return args


class QwenModel:
    def __init__(self, config: QwenConfig | None = None, transport: Transport | None = None):
        self.config = config or QwenConfig()
        self.name = f"{self.config.served_model} temperature={self.config.temperature}"
        self._transport = transport or post_json

    def system_text(self) -> str:
        return self.config.system_prompt + TOOLS_TEXT + RESPONSE_FORMAT

    def user_text(self, obs: Observation) -> str:
        size = obs.screenshot.size
        history = obs.history[-self.config.history_steps:]
        if history:
            lines = []
            for entry in history:
                done = "; ".join(describe_relative(a, size) for a in entry.actions) or "no action"
                effect = " -> the screen did not change" if entry.unchanged else ""
                note = f" ({entry.thought})" if entry.thought else ""
                lines.append(f"Step {entry.step}: {done}{effect}{note}")
            progress = f"Task progress (your recent steps, coordinates on the 0-{SCALE} scale):\n" + "\n".join(lines)
        else:
            progress = "Task progress: this is the first step."
        parts = [f"The user query: {self.config.goal or DEFAULT_GOAL}", f"Step {obs.step}.", progress]
        notes = getattr(obs, "notes", "")
        if notes:
            parts.append(f"Notes:\n{notes}")
        return "\n\n".join(parts)

    def build_request(self, obs: Observation) -> tuple[dict, dict]:
        """(request, the same request with the image replaced by a placeholder, for the log)."""
        png = _png(obs.screenshot)
        image_url = "data:image/png;base64," + base64.standard_b64encode(png).decode("ascii")

        def messages(url: str) -> list[dict]:
            return [
                {"role": "system", "content": [{"type": "text", "text": self.system_text()}]},
                {"role": "user", "content": [
                    {"type": "text", "text": self.user_text(obs)},
                    {"type": "image_url", "image_url": {"url": url}},
                ]},
            ]

        c = self.config
        params = {
            "model": c.served_model,
            "max_tokens": c.max_tokens,
            "temperature": c.temperature,
            "top_p": c.top_p,
            "top_k": c.top_k,  # vLLM sampling extension
        }
        if c.seed is not None:
            params["seed"] = c.seed
        if c.logprobs:
            params["logprobs"] = True
            if c.top_logprobs:
                params["top_logprobs"] = c.top_logprobs
        if c.return_token_ids:
            params["return_token_ids"] = True
        width, height = obs.screenshot.size
        placeholder = f"<screenshot {width}x{height} png sha256={hashlib.sha256(png).hexdigest()}>"
        return {**params, "messages": messages(image_url)}, {**params, "messages": messages(placeholder)}

    def decide(self, obs: Observation) -> Decision:
        request, logged = self.build_request(obs)
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        response = self._transport(url, request, headers, self.config.timeout)
        return self.parse(response, obs, logged)

    def parse(self, response: dict, obs: Observation, logged_request: dict | None = None) -> Decision:
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError(f"vLLM response has no choices: {json.dumps(response)[:500]}")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reasoning, thought, bodies = split_reply(text)
        reasoning = message.get("reasoning_content") or message.get("reasoning") or reasoning
        # A server started with a tool-call parser moves the calls out of content.
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            bodies.append(json.dumps({"name": fn.get("name"), "arguments": fn.get("arguments", "{}")}))

        size = obs.screenshot.size
        earlier = [a for entry in obs.history for a in entry.actions]
        pointer = next((p for p in map(_pointer_after, reversed(earlier)) if p), None)
        actions: list[Action] = []
        calls, skipped, errors = [], [], []
        terminate = answer = None
        for body in bodies:
            try:
                args = parse_call(body)
            except (ValueError, json.JSONDecodeError) as exc:
                log.warning("unparsed tool call %r: %s", body[:200], exc)
                errors.append(f"{exc}: {body[:200]}")
                continue
            calls.append(args)
            name = args.get("action")
            if name == "terminate":
                terminate = str(args.get("status", "success"))
                continue
            if name == "answer":
                answer = str(args.get("text", ""))
                continue
            try:
                action = to_action(args, size, pointer)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("bad arguments %r: %s", args, exc)
                action = None
            if action is None:
                skipped.append(str(name))
                continue
            actions.append(action)
            pointer = _pointer_after(action) or pointer

        if answer:
            thought = (thought + f" Answer: {answer}").strip()
        if skipped:
            thought = (thought + f" [skipped unsupported: {', '.join(skipped)}]").strip()
        if errors:
            thought = (thought + f" [unparsed tool call x{len(errors)}]").strip()
        if not bodies and text.strip():
            log.warning("reply has no tool call: %r", text[:200])

        finish = choice.get("finish_reason")
        if terminate is not None:
            stop_reason = f"terminate_{terminate}"
        elif answer is not None:
            stop_reason = "answer"
        else:
            stop_reason = finish
        done = terminate is not None or answer is not None
        if not bodies and not actions:
            done = re.search(r"\bDONE\b", thought) is not None

        u = response.get("usage") or {}
        usage = Usage(
            input_tokens=u.get("prompt_tokens") or 0,
            output_tokens=u.get("completion_tokens") or 0,
            cache_read_input_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
            cost_usd=0.0,
        )
        logprobs = choice.get("logprobs") or {}
        raw = {
            "request": logged_request,
            "response_text": text,
            "id": response.get("id"),
            "model": response.get("model"),
            "finish_reason": finish,
            "usage": u,
            "logprobs": logprobs.get("content"),
            "prompt_token_ids": response.get("prompt_token_ids") or choice.get("prompt_token_ids"),
            "token_ids": choice.get("token_ids"),
            "tool_calls": calls,  # arguments as the model wrote them, 0-1000 coordinates
            "parse_errors": errors,
        }
        return Decision(
            thought=thought,
            actions=actions,
            reasoning=reasoning,
            stop_reason=stop_reason,
            done=done,
            usage=usage,
            raw=raw,
        )
