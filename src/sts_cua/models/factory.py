"""Builds a Model from command-line flags, so every CLI picks models the same way.

    parser = argparse.ArgumentParser()
    add_model_args(parser)
    args = parser.parse_args()
    model = build_model(args)

--model is 'scripted' (no model; waits every step), a local model name from registry.MODELS
(served by vLLM or llama-server, see docs/hosting.md), or anything else, which is taken as a
Claude model ID. A local model's adapter is picked by its registry `family`, and the server
is asked for its registry `served_name` unless --served-model says otherwise.
build_model reads args.max_steps for the scripted model when the parser defines it, and
args.coop (python -m sts_cua --coop) to add the co-op prompt; parsers without it get solo.

--local-thinking on|off sets enable_thinking in the chat template. Left unset it is off in
co-op (speed) and otherwise the registry's default for the model (`thinking`).
"""

from __future__ import annotations

import argparse
import importlib
import os

from ..actions import Wait
from . import registry
from .base import Model, ScriptedModel

# --model name: the name its server answers to (vLLM: the HF repo id; llama-server: --alias).
LOCAL_MODELS = {name: m.served_name for name, m in registry.MODELS.items()}


def adapter_class(family: str):
    """The adapter for a registry family."""
    if family == "qwen3-vl":
        from .qwen import QwenModel
        return QwenModel
    if family == "qwen3.8":
        from .qwen38 import Qwen38Model
        return Qwen38Model
    if family == "holo4":
        from .holo4 import Holo4Model
        return Holo4Model
    if family == "ui-venus-2":
        from .ui_venus import VenusModel
        return VenusModel
    raise ValueError(f"no adapter for model family {family!r}")


def local_thinking(args: argparse.Namespace, model: registry.LocalModel) -> bool | None:
    """enable_thinking for this run: the flag, else off in co-op, else the registry default.

    None for models without a thinking mode (nothing is sent to the template).
    """
    flag = getattr(args, "local_thinking", None)
    if not model.thinking:
        if flag == "on":
            raise ValueError(f"{model.name} has no thinking mode")
        return None
    if flag is not None:
        return flag == "on"
    if getattr(args, "coop", False):
        return False
    return model.thinking


def add_model_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("model")
    g.add_argument("--model", default="claude-haiku-5-5",
                   help=f"Claude model ID, a local model ({', '.join(registry.MODELS)}), or 'scripted' for a no-model dry run")
    g.add_argument("--history-steps", type=int, default=8, help="recent steps shown to the model")
    g.add_argument("--goal", help="goal shown to the model every step, e.g. 'Start a singleplayer run'")
    g.add_argument("--prompt-file", help="replace the Spire system prompt, e.g. prompts/click_target.txt")
    g.add_argument("--max-tokens", type=int, help="output token cap (default: the adapter's own)")
    # Claude only.
    g.add_argument("--thinking", choices=["on", "off"], default="off", help="Claude only")
    g.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"], help="Claude only")
    g.add_argument("--send-width", type=int, help="Claude only: shrink screenshots to this width before sending, e.g. 960")
    # Local model only.
    g.add_argument("--base-url", default="http://localhost:8000/v1",
                   help="local model: OpenAI-compatible endpoint (vLLM or llama-server)")
    g.add_argument("--served-model", help="local model: name the server answers to (default: the registry's), or a LoRA adapter")
    g.add_argument("--temperature", type=float, help="local model: sampling temperature (default: the model family's)")
    g.add_argument("--seed", type=int, help="local model: sampling seed")
    g.add_argument("--local-thinking", choices=["on", "off"],
                   help="local model: thinking mode (default: off in co-op, else the registry's default for the model)")
    g.add_argument("--local-effort", choices=["low", "medium", "xhigh"],
                   help="local model with thinking on: reasoning_effort for the chat template (default: the family's)")


def _prompt(args: argparse.Namespace) -> str | None:
    if not getattr(args, "prompt_file", None):
        return None
    with open(args.prompt_file, encoding="utf-8") as f:
        return f.read().strip()


def build_model(args: argparse.Namespace) -> Model:
    prompt = _prompt(args)
    if args.model == "scripted":
        return ScriptedModel([[Wait(0.1)]] * (getattr(args, "max_steps", None) or 1000))

    if args.model in registry.MODELS:
        return build_local(args, registry.MODELS[args.model], prompt)

    from .claude import ClaudeConfig, ClaudeModel

    config = ClaudeConfig(
        model=args.model, thinking=args.thinking, effort=args.effort, history_steps=args.history_steps,
        send_width=args.send_width, goal=args.goal, max_tokens=args.max_tokens,
        coop=bool(getattr(args, "coop", False)),
    )
    if prompt:
        config.system_prompt = prompt
    return ClaudeModel(config)


def build_local(args: argparse.Namespace, model: registry.LocalModel, prompt: str | None = None) -> Model:
    """The adapter for a registry model, configured from the shared flags."""
    cls = adapter_class(model.family)
    config = cls.config_class(
        base_url=args.base_url,
        served_model=args.served_model or model.served_name,
        api_key=os.environ.get("VLLM_API_KEY") or os.environ.get("LLAMA_API_KEY") or None,
        history_steps=args.history_steps,
        goal=args.goal,
        seed=args.seed,
        coop=bool(getattr(args, "coop", False)),
        thinking=local_thinking(args, model),
        reasoning_effort=getattr(args, "local_effort", None),
    )
    if args.temperature is not None:
        config.temperature = args.temperature
    if args.max_tokens:
        config.max_tokens = args.max_tokens
    if model.server != "vllm":
        config.return_token_ids = False  # vLLM extension
    if prompt:
        config.system_prompt = importlib.import_module(cls.__module__).adapt_prompt(prompt)
    return cls(config)
