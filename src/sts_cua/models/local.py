"""Shared base for local models behind an OpenAI-compatible chat completions server.

The servers are vLLM (the Qwen3-VL baseline) and llama.cpp's llama-server (everything in
registry.MODELS with server="llama"). Both take POST {base_url}/chat/completions with the
screenshot as a base64 data URL, and both pass `chat_template_kwargs` to the model's Jinja
chat template, which is how thinking is switched on and off:

- llama.cpp tools/server/README.md (read 2026-10-25): "`chat_template_kwargs`: Allows
  sending additional parameters to the json templating system. For example:
  `{"enable_thinking": false}`"; `--jinja` is now on by default, and function calling
  (`tools`) needs it. With the default `--reasoning-format auto`, thinking comes back in
  `message.reasoning_content`; with `none` it stays in `content` as <think>...</think>.
  Every response carries `timings` (prompt_n, prompt_per_second, predicted_n,
  predicted_per_second, cache_n, ...), which the bench reads from Decision.raw.
- The Qwen3.5+ chat template (Qwen/Qwen3.8-27B chat_template.jinja; Holo4-27B and
  UI-Venus-2-9B ship the same logic) opens "<think>\n" in the generation prompt when
  thinking is on, so only the closing </think> arrives when a server leaves it in content,
  and pre-fills an empty "<think>\n\n</think>\n\n" when enable_thinking is false. It also
  reads `reasoning_effort` (xhigh, the default; medium; low) from the template kwargs.

Every step is a fresh request like the Claude adapter: a system prompt, then one user turn
with the goal, a short text log of recent steps, any notes, and the current screenshot.
Thinking text goes to Decision.reasoning and the run log, never into the next step's
history (that is built from Decision.thought). Subclasses supply the prompt, the user turn,
how a reply is read into calls, and how each call becomes actions.
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
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image

from ..actions import Action, Click, Drag, MouseMove, Scroll, describe
from .base import Decision, Observation, Usage

log = logging.getLogger(__name__)

MAX_SCROLL = 10  # wheel clicks per scroll action

# (url, payload, headers, timeout) -> response JSON. Swapped out in tests.
Transport = Callable[[str, dict, dict, float], dict]


def post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:2000]
        raise RuntimeError(f"model server returned HTTP {exc.code}: {body}") from exc


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---- coordinates

def scale_point(value, screen_size: tuple[int, int], scale: int, floor: bool = False) -> tuple[int, int] | None:
    """A model (x, y) on a 0-`scale` grid to screen pixels, clamped to the screen.

    value is a pair, a "(x, y)" string, or a 4-number box (its centre). floor truncates like
    the families' own reference code (int(x / scale * width)); otherwise it rounds.
    """
    if isinstance(value, str):
        value = re.findall(r"-?\d+(?:\.\d+)?", value)
    if isinstance(value, dict) and "x" in value and "y" in value:
        value = (value["x"], value["y"])
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            value = ((float(value[0]) + float(value[2])) / 2, (float(value[1]) + float(value[3])) / 2)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(v, bool) for v in value):
        return None
    try:
        rx, ry = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None
    if rx != rx or ry != ry or abs(rx) == float("inf") or abs(ry) == float("inf"):
        return None
    width, height = screen_size
    fx, fy = rx / scale * width, ry / scale * height
    x, y = (int(fx), int(fy)) if floor else (round(fx), round(fy))
    return min(max(x, 0), width - 1), min(max(y, 0), height - 1)


def to_scale(x: int, y: int, screen_size: tuple[int, int], scale: int) -> tuple[int, int]:
    return round(x / screen_size[0] * scale), round(y / screen_size[1] * scale)


def describe_scaled(action: Action, screen_size: tuple[int, int], scale: int) -> str:
    """describe(), with coordinates on the model's 0-`scale` grid."""
    if isinstance(action, (Click, MouseMove)) or (isinstance(action, Scroll) and action.x is not None):
        x, y = to_scale(action.x, action.y, screen_size, scale)
        action = dataclasses.replace(action, x=x, y=y)
    elif isinstance(action, Drag):
        x0, y0 = to_scale(action.x0, action.y0, screen_size, scale)
        x1, y1 = to_scale(action.x1, action.y1, screen_size, scale)
        action = Drag(x0, y0, x1, y1)
    return describe(action)


def pointer_after(action: Action) -> tuple[int, int] | None:
    if isinstance(action, (Click, MouseMove)):
        return action.x, action.y
    if isinstance(action, Drag):
        return action.x1, action.y1
    if isinstance(action, Scroll) and action.x is not None:
        return action.x, action.y
    return None


# ---- keys

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


def join_keys(keys) -> str | None:
    """["ctrl", "Enter"], "ctrl+enter" or "e" to an xdotool chord, or None if empty."""
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, (list, tuple)) or not keys:
        return None
    names = [part for key in keys for part in str(key).split("+") if part.strip()]
    return "+".join(to_keysym(n) for n in names) or None


def scroll_amount(value) -> int:
    """Wheel clicks for a model's scroll size, at least 1 and at most MAX_SCROLL."""
    return min(max(round(abs(float(value))), 1), MAX_SCROLL)


# ---- reading replies

_THINK = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL)


def split_think(text: str) -> tuple[str, str]:
    """(reasoning inside <think>, the rest). A lone </think> closes a block the template opened."""
    reasoning = " ".join(m.strip() for m in _THINK.findall(text) if m.strip())
    text = _THINK.sub("", text)
    if "</think>" in text:
        head, text = text.split("</think>", 1)
        reasoning = " ".join(p for p in (head.strip(), reasoning) if p)
    return reasoning, text


def server_reasoning(message: dict) -> str:
    """Thinking the server split out: llama-server and vLLM's reasoning parsers."""
    return (message.get("reasoning_content") or message.get("reasoning") or "").strip()


def server_tool_calls(message: dict) -> list[str]:
    """Tool calls a server parsed out of the reply, as JSON bodies like the ones in content."""
    bodies = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        bodies.append(json.dumps({"name": fn.get("name"), "arguments": fn.get("arguments", "{}")}))
    return bodies


_XML_FUNCTION = re.compile(r"<function=([^>\n]+)>(.*?)(?:</function>|(?=<function=)|\Z)", re.DOTALL)
_XML_PARAMETER = re.compile(r"<parameter=([^>\n]+)>\n?(.*?)(?:\n?</parameter>|(?=<parameter=)|(?=</function>)|\Z)",
                            re.DOTALL)


def xml_value(raw: str):
    """A <parameter> value: JSON when it parses (numbers, lists, true/false), else the text."""
    value = raw.strip("\n")
    stripped = value.strip()
    if stripped and (stripped[0] in "[{-\"" or stripped[0].isdigit() or stripped in ("true", "false", "null")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return value


def parse_tool_body(body: str) -> tuple[str | None, dict]:
    """(function name, arguments) of one tool call body: Qwen3.5+ XML or the older JSON form.

    XML, from the Qwen3.8 chat template:
        <function=computer_use>
        <parameter=action>
        left_click
        </parameter>
        ...
        </function>
    JSON, from Qwen3-VL's NousFnCallPrompt: {"name": ..., "arguments": {...}}.
    Raises ValueError (or json.JSONDecodeError) when neither form parses.
    """
    match = _XML_FUNCTION.search(body)
    if match:
        args = {m.group(1).strip(): xml_value(m.group(2)) for m in _XML_PARAMETER.finditer(match.group(2))}
        return match.group(1).strip(), args
    try:
        call = json.loads(body)
    except json.JSONDecodeError:
        # Trailing junk after the object, e.g. a missing </tool_call> before more text.
        call, _ = json.JSONDecoder().raw_decode(body)
    if not isinstance(call, dict):
        raise ValueError("tool call is not a JSON object")
    args = call.get("arguments", {})
    if isinstance(args, str):
        args = json.loads(args) if args.strip() else {}
    if not isinstance(args, dict):
        raise ValueError("arguments are not an object")
    return call.get("name"), args


_TOOL_CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|\Z)", re.DOTALL)
_BARE_FUNCTION = re.compile(r"<function=[^>\n]+>.*?(?:</function>|(?=<function=)|\Z)", re.DOTALL)


def split_tool_calls(text: str) -> tuple[str, list[str]]:
    """(text before the first call, each call's body). Bare <function=...> blocks count too."""
    calls = [m.strip() for m in _TOOL_CALL.findall(text)]
    if not calls:
        calls = [m.strip() for m in _BARE_FUNCTION.findall(text)]
    before = re.split(r"<tool_call>|<function=", text, maxsplit=1)[0]
    return " ".join(before.split()), calls


# ---- the adapter

@dataclass
class Reply:
    """A reply read into calls, before they become actions."""
    thought: str
    reasoning: str
    calls: list = field(default_factory=list)  # family-specific dicts, logged as raw["tool_calls"]
    errors: list = field(default_factory=list)  # bodies that didn't parse


@dataclass(frozen=True)
class Finish:
    """A call that ends the task (terminate, answer, Finished, ...)."""
    reason: str  # the Decision's stop_reason
    text: str = ""
    label: str = "Answer"  # how text is added to the thought


@dataclass
class Cursor:
    """Pointer state while one reply's calls are converted."""
    pointer: tuple[int, int] | None = None
    held: tuple[int, int] | None = None  # where a mouse_down happened, until its mouse_up


@dataclass
class LocalConfig:
    base_url: str = "http://localhost:8000/v1"
    served_model: str = ""  # the server's model name (llama-server --alias, vLLM --served-model-name)
    api_key: str | None = None  # only if the server runs with --api-key
    temperature: float | None = None  # None: the family's default for the thinking mode
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    seed: int | None = None
    history_steps: int = 8
    goal: str | None = None
    system_prompt: str = ""  # empty: the family's Spire prompt
    logprobs: bool = True
    top_logprobs: int = 0  # alternatives per token, 0 to 20
    return_token_ids: bool = False  # vLLM extension; servers without it ignore the field
    timeout: float = 300.0
    coop: bool = False  # co-op with a human teammate: coop.PROMPT goes after the system prompt
    # None sends no chat_template_kwargs (the template's default); True/False sets enable_thinking.
    thinking: bool | None = None
    reasoning_effort: str | None = None  # xhigh, medium or low, sent with thinking on


DEFAULT_GOAL = "Play Slay the Spire 2 and win the run."


class LocalAdapter:
    """Request building, the HTTP call and the reply-to-Decision plumbing shared by the families."""

    family = ""
    scale = 1000  # the model's coordinate grid
    floor = False  # truncate scaled coordinates like the family's reference code
    # thinking on/off -> (temperature, top_p, top_k) when the config leaves them None
    sampling_defaults: dict = {False: (0.7, 0.8, 20), True: (1.0, 0.95, 20)}
    max_tokens_defaults: dict = {False: 1024, True: 8192}
    default_effort: str | None = None  # reasoning_effort when thinking is on and none is set
    system_prompt = ""  # the family's Spire prompt
    config_class = LocalConfig

    def __init__(self, config: LocalConfig | None = None, transport: Transport | None = None):
        self.config = config or self.config_class()
        if not self.config.system_prompt:
            self.config.system_prompt = self.system_prompt
        temperature, _, _ = self.sampling()
        thinking = "" if self.config.thinking is None else f" thinking={'on' if self.config.thinking else 'off'}"
        self.name = f"{self.config.served_model}{thinking} temperature={temperature}"
        self._transport = transport or post_json

    # -- hooks

    def system_text(self) -> str:
        raise NotImplementedError

    def user_content(self, obs: Observation, image_url: str) -> list[dict]:
        raise NotImplementedError

    def read_reply(self, text: str, message: dict) -> Reply:
        raise NotImplementedError

    def convert(self, call, screen_size: tuple[int, int], cursor: Cursor) -> list | None:
        """One call to actions and Finish markers ([] when it only changes cursor state), or None when it can't run here."""
        raise NotImplementedError

    def call_name(self, call) -> str:
        return str(call.get("name") or call.get("action")) if isinstance(call, dict) else str(call)

    def extra_params(self) -> dict:
        """Family-specific request fields, e.g. tools."""
        return {}

    # -- shared

    def point(self, value, screen_size: tuple[int, int]) -> tuple[int, int] | None:
        return scale_point(value, screen_size, self.scale, self.floor)

    def history_lines(self, obs: Observation) -> list[str]:
        size = obs.screenshot.size
        lines = []
        for entry in obs.history[-self.config.history_steps:]:
            done = "; ".join(describe_scaled(a, size, self.scale) for a in entry.actions) or "no action"
            effect = " -> the screen did not change" if entry.unchanged else ""
            note = f" ({entry.thought})" if entry.thought else ""
            lines.append(f"Step {entry.step}: {done}{effect}{note}")
        return lines

    def sampling(self) -> tuple[float, float, int | None]:
        c = self.config
        t, p, k = self.sampling_defaults[bool(c.thinking)]
        return (t if c.temperature is None else c.temperature, p if c.top_p is None else c.top_p,
                k if c.top_k is None else c.top_k)

    def template_kwargs(self) -> dict:
        c = self.config
        if c.thinking is None:
            return {}
        kwargs = {"enable_thinking": bool(c.thinking)}
        effort = c.reasoning_effort or self.default_effort
        if c.thinking and effort:
            kwargs["reasoning_effort"] = effort
        return kwargs

    def request_params(self) -> dict:
        c = self.config
        temperature, top_p, top_k = self.sampling()
        params = {
            "model": c.served_model,
            "max_tokens": c.max_tokens or self.max_tokens_defaults[bool(c.thinking)],
            "temperature": temperature,
            "top_p": top_p,
        }
        if top_k is not None:
            params["top_k"] = top_k  # vLLM and llama-server sampling extension
        if c.seed is not None:
            params["seed"] = c.seed
        if c.logprobs:
            params["logprobs"] = True
            if c.top_logprobs:
                params["top_logprobs"] = c.top_logprobs
        if c.return_token_ids:
            params["return_token_ids"] = True
        kwargs = self.template_kwargs()
        if kwargs:
            params["chat_template_kwargs"] = kwargs
        params.update(self.extra_params())
        return params

    def build_request(self, obs: Observation) -> tuple[dict, dict]:
        """(request, the same request with the image replaced by a placeholder, for the log)."""
        png = png_bytes(obs.screenshot)
        image_url = "data:image/png;base64," + base64.standard_b64encode(png).decode("ascii")

        def messages(url: str) -> list[dict]:
            return [
                {"role": "system", "content": [{"type": "text", "text": self.system_text()}]},
                {"role": "user", "content": self.user_content(obs, url)},
            ]

        params = self.request_params()
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
            raise RuntimeError(f"model server response has no choices: {json.dumps(response)[:500]}")
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        reply = self.read_reply(text, message)
        for error in reply.errors:
            log.warning("unparsed tool call: %s", error)

        size = obs.screenshot.size
        earlier = [a for entry in obs.history for a in entry.actions]
        cursor = Cursor(next((p for p in map(pointer_after, reversed(earlier)) if p), None))
        actions: list[Action] = []
        finishes: list[Finish] = []
        skipped: list[str] = []
        for call in reply.calls:
            try:
                out = self.convert(call, size, cursor)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                log.warning("bad arguments %r: %s", call, exc)
                out = None
            if out is None:
                skipped.append(self.call_name(call))
                continue
            for item in out:
                if isinstance(item, Finish):
                    finishes.append(item)
                    continue
                actions.append(item)
                cursor.pointer = pointer_after(item) or cursor.pointer

        thought = reply.thought
        for finish in finishes:
            if finish.text:
                thought = (thought + f" {finish.label}: {finish.text}").strip()
        if skipped:
            thought = (thought + f" [skipped unsupported: {', '.join(skipped)}]").strip()
        if reply.errors:
            thought = (thought + f" [unparsed tool call x{len(reply.errors)}]").strip()
        if not reply.calls and not reply.errors and text.strip():
            log.warning("reply has no action: %r", text[:200])

        finish_reason = choice.get("finish_reason")
        done = bool(finishes)
        if finishes:
            stop_reason = next((f.reason for f in finishes if f.reason.startswith("terminate")), finishes[0].reason)
        else:
            stop_reason = finish_reason
        if not reply.calls and not reply.errors and not actions:
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
            "reasoning_content": server_reasoning(message) or None,  # as the server split it out
            "id": response.get("id"),
            "model": response.get("model"),
            "finish_reason": finish_reason,
            "usage": u,
            "timings": response.get("timings"),  # llama-server: prompt/predicted tokens and speeds
            "logprobs": logprobs.get("content"),
            "prompt_token_ids": response.get("prompt_token_ids") or choice.get("prompt_token_ids"),
            "token_ids": choice.get("token_ids"),
            "tool_calls": reply.calls,  # as the model wrote them, on its own coordinate grid
            "parse_errors": reply.errors,
        }
        return Decision(
            thought=thought,
            actions=actions,
            reasoning=reply.reasoning,
            stop_reason=stop_reason,
            done=done,
            usage=usage,
            raw=raw,
        )


def adapt_prompt(prompt: str, edits, name: str, strict: bool = False) -> str:
    """Apply (old, new) edits that turn the Claude Spire prompt into one for a family.

    strict: every edit must apply (tests use it so the prompt can't drift silently).
    """
    for old, new in edits:
        if old in prompt:
            prompt = prompt.replace(old, new)
        elif strict:
            raise ValueError(f"Spire prompt changed; update {name} prompt edits for: {old!r}")
    return prompt
