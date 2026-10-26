"""Qwen3.8 adapter (Qwen3.8-27B GGUF on llama-server), on Qwen's own OSWorld scaffold.

Qwen3.8 publishes no computer-use cookbook: its model card (huggingface.co/Qwen/Qwen3.8-27B,
read 2026-10-26) gives OSWorld-Verified 84.3 "under the OSWorld scaffold" and no prompt,
and QwenLM/Qwen3.5 (which hosts Qwen3.6 and 3.8, @ 2ea10dc) has none either. The scaffold
is Qwen's agent in the OSWorld repo, mm_agents/qwen (xlang-ai/OSWorld @ 044bc33,
2026-10-24; paper: Qwen-CUA, arXiv 2608.02352), which this follows:

- The same `computer_use` function as Qwen3-VL (prompts.build_base_tools_def, copied below
  with coordinate_type="relative": "The screen's resolution is 1000x1000."), plus an
  optional `text` of held modifier keys on clicks.
- What changed from Qwen3-VL is the call format. The tools go in the system prompt in the
  Qwen3.5+ chat template's own words (the "# Tools ... <tools>" block and the XML reply
  format, as rendered by Qwen/Qwen3.8-27B chat_template.jinja), and the model replies
      <tool_call>
      <function=computer_use>
      <parameter=action>
      left_click
      </parameter>
      <parameter=coordinate>
      [500, 500]
      </parameter>
      </function>
      </tool_call>
  The scaffold's parser (parser.py) also accepts Qwen3-VL's JSON form; so does this one.
- Coordinates: images.adjust_coordinates maps relative ones as int(x * width / 999).
- The user turn is the screenshot, then "Please generate the next move according to the UI
  screenshot, instruction and previous actions." with the instruction and earlier steps.
- Sampling: the scaffold defaults to temperature 0.0, top_p 0.9 with thinking off. With
  thinking on, the model card's thinking values (temperature 1.0, top_p 0.95, top_k 20);
  presence_penalty is left off as in qwen.py.
- Thinking is on by default upstream; the adapter always sends chat_template_kwargs
  {"enable_thinking": ...} (model card's non-thinking example), which llama-server passes to
  the template. With it on, llama-server returns the thinking in reasoning_content.

UNVERIFIED: that Qwen3.8 was trained on this exact tool text (the scaffold is used for
qwen3.7-plus in its README; Qwen3.8 isn't named), and the 999 divisor (Qwen3-VL's cookbook
divides by 1000; the difference is under 0.1%). The response format keeps a Thought line
and allows several calls per step, like qwen.py, where the scaffold asks for one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .base import Observation
from .claude import SYSTEM_PROMPT as _CLAUDE_PROMPT
from .local import DEFAULT_GOAL, LocalConfig, adapt_prompt as _adapt
from .qwen import QwenModel, _PROMPT_EDITS

SCALE = 1000  # what the tool tells the model
DIVISOR = 999  # what the scaffold divides by

_ACTIONS_TEXT = """
* `key`: Performs key down presses on the arguments passed in order, then performs key releases in reverse order.
* `type`: Type a string of text on the keyboard.
* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.
* `left_click`: Click the left mouse button at a specified (x, y) pixel coordinate on the screen. Optional `text` parameter can specify modifier keys (e.g., "ctrl", "shift", "ctrl+shift") that will be held during the click.
* `left_click_drag`: Click and drag the cursor to a specified (x, y) coordinate.
* `right_click`: Click the right mouse button at a specified (x, y) pixel coordinate on the screen. Optional `text` parameter can specify modifier keys that will be held during the click.
* `middle_click`: Click the middle mouse button at a specified (x, y) pixel coordinate on the screen. Optional `text` parameter can specify modifier keys that will be held during the click.
* `double_click`: Double-click the left mouse button at a specified (x, y) pixel coordinate on the screen. Optional `text` parameter can specify modifier keys that will be held during the click.
* `triple_click`: Triple-click the left mouse button at a specified (x, y) pixel coordinate on the screen (simulated as double-click since it's the closest action). Optional `text` parameter can specify modifier keys that will be held during the click.
* `scroll`: Performs a scroll of the mouse scroll wheel. Optional `text` parameter can specify a modifier key (e.g., "shift", "ctrl") that will be held during scrolling.
* `hscroll`: Performs a horizontal scroll (mapped to regular scroll). Optional `text` parameter can specify a modifier key that will be held during scrolling.
* `wait`: Wait specified seconds for the change to happen.
* `terminate`: Terminate the current task and report its completion status.
* `answer`: Answer a question."""

# Verbatim from build_base_tools_def(..., coordinate_type="relative") in mm_agents/qwen/prompts.py.
COMPUTER_USE_TOOL = {
    "type": "function",
    "function": {
        "name": "computer_use",
        "description": "\n".join([
            "Use a mouse and keyboard to interact with a computer, and take screenshots.",
            "* This is an interface to a desktop GUI. You do not have access to a terminal or applications menu. You must click on desktop icons to start applications.",
            "* Some applications may take time to start or process actions, so you may need to wait and take successive screenshots to see the results of your actions.",
            f"* The screen's resolution is {SCALE}x{SCALE}.",
            "* Whenever you intend to move the cursor to click on an element like an icon, you should consult a screenshot to determine the coordinates of the element before moving the cursor.",
            "* If you tried clicking on a program or link but it failed to load, even after waiting, try adjusting your cursor position so that the tip of the cursor visually falls on the element that you want to click.",
            "* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked.",
        ]),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "description": _ACTIONS_TEXT,
                    "enum": [
                        "key", "type", "mouse_move", "left_click", "left_click_drag", "right_click", "middle_click",
                        "double_click", "triple_click", "scroll", "hscroll", "wait", "terminate", "answer",
                    ],
                },
                "keys": {"type": "array", "description": "Required only by `action=key`."},
                "text": {
                    "type": "string",
                    "description": "Required by `action=type` and `action=answer`. Optional for click actions (left_click, right_click, middle_click, double_click, triple_click) to specify modifier keys (e.g., 'ctrl', 'shift', 'ctrl+shift'). Optional for scroll actions (scroll, hscroll) to specify a modifier key (e.g., 'shift', 'ctrl') to hold during scrolling.",
                },
                "coordinate": {"type": "array", "description": "(x, y) coordinates."},
                "pixels": {"type": "number", "description": "Scroll amount."},
                "time": {"type": "number", "description": "Seconds to wait."},
                "status": {"type": "string", "description": "Task status for terminate.", "enum": ["success", "failure"]},
            },
        },
    },
}

# build_base_system_prompt's tool section, which matches the Qwen3.8 chat template's own
# rendering of `tools`; the scaffold's date and collapsed-screenshot lines are dropped
# (every request here has one screenshot).
TOOLS_TEXT = (
    "\n\n# Tools\n\nYou have access to the following functions:\n\n<tools>\n"
    + json.dumps(COMPUTER_USE_TOOL)
    + "\n</tools>\n\n"
    "If you choose to call a function ONLY reply in the following format with NO suffix:\n\n"
    "<tool_call>\n<function=example_function_name>\n<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
    "<parameter=example_parameter_2>\nThis is the value for the second parameter\nthat can span\nmultiple lines\n"
    "</parameter>\n</function>\n</tool_call>\n\n"
    "<IMPORTANT>\nReminder:\n"
    "- Function calls MUST follow the specified format: an inner <function=...></function> block must be nested within <tool_call></tool_call> XML tags\n"
    "- Required parameters MUST be specified\n"
    "- You may provide optional reasoning for your function call in natural language BEFORE the function call, but NOT after\n"
    "- If there is no function call available, answer the question like normal with your current knowledge and do not tell the user about function calls\n"
    "</IMPORTANT>"
)

# The scaffold's "# Response format", with a Thought line and several calls allowed.
RESPONSE_FORMAT = """

# Response format

Response format for every step:
1) Thought: one or two short sentences on what you see and why you take the next action.
2) Action: a short imperative describing what to do in the game.
3) One or more <tool_call>...</tool_call> blocks, each with one computer_use call. They run in order.

Rules:
- Output exactly in the order: Thought, Action, <tool_call>.
- Be brief: one sentence each for Thought and Action.
- Do not output anything after the tool calls.
- If finishing, use action=terminate in the tool call."""


def adapt_prompt(prompt: str, strict: bool = False) -> str:
    """The Qwen3-VL edits apply unchanged: same tool, same 0-1000 wording."""
    return _adapt(prompt, _PROMPT_EDITS, "qwen._PROMPT_EDITS", strict)


SYSTEM_PROMPT = adapt_prompt(_CLAUDE_PROMPT)


@dataclass
class Qwen38Config(LocalConfig):
    served_model: str = "qwen3.8-27b"  # llama-server --alias (registry.MODELS served_name)


class Qwen38Model(QwenModel):
    family = "qwen3.8"
    scale = DIVISOR
    floor = True
    # Thinking off: the OSWorld scaffold's defaults. On: the model card's thinking values.
    sampling_defaults = {False: (0.0, 0.9, None), True: (1.0, 0.95, 20)}
    max_tokens_defaults = {False: 1024, True: 8192}
    system_prompt = SYSTEM_PROMPT
    config_class = Qwen38Config
    tools_text = TOOLS_TEXT
    response_format = RESPONSE_FORMAT

    def user_text(self, obs: Observation) -> str:
        lines = self.history_lines(obs)
        previous = "\n".join(lines) if lines else "None"
        parts = [
            "Please generate the next move according to the UI screenshot, instruction and previous actions.",
            f"Instruction: {self.config.goal or DEFAULT_GOAL}",
            f"Step {obs.step}.",
            f"Previous actions (coordinates on the 0-{SCALE} scale):\n{previous}",
        ]
        if obs.notes:
            parts.append(f"Notes:\n{obs.notes}")
        return "\n\n".join(parts)

    def user_content(self, obs: Observation, image_url: str) -> list[dict]:
        # Screenshot first, as in the scaffold's history.build_messages.
        return [
            {"type": "image_url", "image_url": {"url": image_url}},
            {"type": "text", "text": self.user_text(obs)},
        ]
