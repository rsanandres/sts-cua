"""Claude adapter using the computer-use toolset.

Every step is a fresh request: system prompt, a short text history of earlier steps,
and the current screenshot. Nothing from earlier responses is replayed, so thinking
blocks never need to be passed back and the cached prefix (tools + system) stays fixed.

Docs: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass

import anthropic
from PIL import Image

from .. import pricing
from ..actions import Action, Click, Drag, Key, MouseMove, Scroll, TypeText, Wait, describe
from .base import Decision, Observation, Usage

# Image limits for the computer toolset models (Claude 4.7 and later).
MAX_LONG_EDGE = 2576
MAX_VISUAL_TOKENS = 4784

SYSTEM_PROMPT = """You are playing Slay the Spire 2 by looking at the screen and using the mouse and keyboard.

Each turn you get one screenshot of the game as it is right now, plus a short log of your recent steps. Take the next action toward winning the run. After your actions run, the next screenshot arrives automatically, so never ask for one.

Coordinates are pixels in the screenshot you were given, origin at the top left.

If the screen is still animating or loading, use wait instead of clicking."""

_THOUGHT_INSTRUCTION = "Before acting, write one or two short sentences saying what you see and why you are taking this action."
_NO_THOUGHT_INSTRUCTION = "Reply with the action only, no explanation."

# Members the harness does not offer: screenshots are sent every step, and the
# low-level press/hold members are not needed for Spire.
_DISABLED_MEMBERS = ("screenshot", "zoom", "cursor_position", "left_mouse_down", "left_mouse_up", "hold_key")

_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class ClaudeConfig:
    model: str = "claude-haiku-5-5"
    thinking: str = "off"  # "off": act directly; "on": adaptive thinking with summaries
    effort: str = "low"
    max_tokens: int | None = None  # default: 16000 with thinking on, 2048 with it off
    history_steps: int = 8


def thinking_params(model: str, thinking: str, effort: str) -> dict:
    if effort not in _EFFORTS:
        raise ValueError(f"effort must be one of {_EFFORTS}")
    output_config = {"effort": effort}
    if thinking == "on":
        return {"thinking": {"type": "adaptive", "display": "summarized"}, "output_config": output_config}
    if thinking != "off":
        raise ValueError("thinking must be 'on' or 'off'")
    if effort in ("xhigh", "max"):
        raise ValueError("thinking off is only accepted at effort high or below")
    if model.startswith("claude-sonnet-5-5"):
        # Sonnet 5.5 rejects {"type": "disabled"}; between_tools turns off up-front thinking.
        return {"thinking": {"type": "between_tools"}, "output_config": output_config}
    if model.startswith(("claude-opus-5-5", "claude-fable-5")):
        raise ValueError(f"{model} cannot run with thinking off")
    return {"thinking": {"type": "disabled"}, "output_config": output_config}


def fit_scale(width: int, height: int) -> float:
    """Largest scale <= 1 that keeps an image inside the model's limits, so the API never resizes it."""
    scale = min(1.0, MAX_LONG_EDGE / max(width, height))
    while math.ceil(width * scale / 28) * math.ceil(height * scale / 28) > MAX_VISUAL_TOKENS:
        scale *= 0.98
    return scale


def _png_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.standard_b64encode(buffer.getvalue()).decode("ascii")


class ClaudeModel:
    def __init__(self, config: ClaudeConfig | None = None, client: anthropic.Anthropic | None = None):
        self.config = config or ClaudeConfig()
        self.name = f"{self.config.model} thinking={self.config.thinking} effort={self.config.effort}"
        self._client = client or anthropic.Anthropic()
        self._thinking = thinking_params(self.config.model, self.config.thinking, self.config.effort)

    def build_request(self, obs: Observation) -> tuple[dict, float]:
        width, height = obs.screenshot.size
        scale = fit_scale(width, height)
        image = obs.screenshot
        if scale < 1.0:
            image = image.resize((round(width * scale), round(height * scale)), Image.LANCZOS)

        instruction = _THOUGHT_INSTRUCTION if self.config.thinking == "on" else _NO_THOUGHT_INSTRUCTION
        history = obs.history[-self.config.history_steps:]
        if history:
            lines = []
            for entry in history:
                done = "; ".join(describe(a) for a in entry.actions) or "no action"
                note = f" ({entry.thought})" if entry.thought else ""
                lines.append(f"Step {entry.step}: {done}{note}")
            history_text = "Your recent steps:\n" + "\n".join(lines)
        else:
            history_text = "This is the first step."

        request = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens or (16000 if self.config.thinking == "on" else 2048),
            "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            "tools": [{
                "type": "computer_toolset_20260801",
                "configs": {member: {"enabled": False} for member in _DISABLED_MEMBERS},
            }],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _png_base64(image)}},
                    {"type": "text", "text": f"Step {obs.step}. Screenshot is {image.width}x{image.height}.\n\n{history_text}\n\n{instruction}"},
                ],
            }],
            **self._thinking,
        }
        return request, scale

    def decide(self, obs: Observation) -> Decision:
        request, scale = self.build_request(obs)
        response = self._client.messages.create(**request)
        return self.parse(response, scale, obs.screenshot.size)

    def parse(self, response, scale: float, screen_size: tuple[int, int]) -> Decision:
        thoughts, reasoning, actions, skipped = [], [], [], []
        for block in response.content:
            if block.type == "text":
                thoughts.append(block.text.strip())
            elif block.type == "thinking":
                if block.thinking:
                    reasoning.append(block.thinking.strip())
            elif block.type == "tool_use" and getattr(block, "toolset_name", None) == "computer":
                action = _to_action(block.name, dict(block.input), scale, screen_size)
                if action is None:
                    skipped.append(block.name)
                else:
                    actions.append(action)

        u = response.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        usage.cost_usd = pricing.cost_usd(self.config.model, usage)

        thought = " ".join(t for t in thoughts if t)
        if skipped:
            thought = (thought + f" [skipped unsupported: {', '.join(skipped)}]").strip()
        raw = response.model_dump(mode="json") if hasattr(response, "model_dump") else None
        return Decision(
            thought=thought,
            actions=actions,
            reasoning="\n\n".join(reasoning),
            stop_reason=response.stop_reason,
            usage=usage,
            raw=raw,
        )


def _point(value, scale: float, screen_size: tuple[int, int]) -> tuple[int, int] | None:
    if not value or len(value) != 2:
        return None
    x = min(max(round(value[0] / scale), 0), screen_size[0] - 1)
    y = min(max(round(value[1] / scale), 0), screen_size[1] - 1)
    return x, y


_CLICKS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}


def _to_action(name: str, args: dict, scale: float, screen_size: tuple[int, int]) -> Action | None:
    if name in _CLICKS:
        point = _point(args.get("coordinate"), scale, screen_size)
        if point is None:
            return None  # a click at the current cursor; the harness always clicks at a point
        button, count = _CLICKS[name]
        return Click(*point, button=button, count=count, modifiers=args.get("text") or None)
    if name == "left_click_drag":
        start = _point(args.get("start_coordinate"), scale, screen_size)
        end = _point(args.get("coordinate"), scale, screen_size)
        return Drag(*start, *end) if start and end else None
    if name == "mouse_move":
        point = _point(args.get("coordinate"), scale, screen_size)
        return MouseMove(*point) if point else None
    if name == "scroll":
        point = _point(args.get("coordinate"), scale, screen_size)
        x, y = point if point else (None, None)
        return Scroll(args["scroll_direction"], int(args.get("scroll_amount", 1)), x, y)
    if name == "key":
        return Key(args["text"], int(args.get("repeat", 1)))
    if name == "type":
        return TypeText(args["text"])
    if name == "wait":
        return Wait(float(args.get("duration", 1)))
    return None
