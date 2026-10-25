"""Builds a Model from command-line flags, so every CLI picks models the same way.

    parser = argparse.ArgumentParser()
    add_model_args(parser)
    args = parser.parse_args()
    model = build_model(args)

--model is 'scripted' (no model; waits every step), a local model name from LOCAL_MODELS
(served by vLLM, see docs/hosting.md), or anything else, which is taken as a Claude model ID.
build_model reads args.max_steps for the scripted model when the parser defines it, and
args.coop (python -m sts_cua --coop) to add the co-op prompt; parsers without it get solo.
"""

from __future__ import annotations

import argparse
import os

from ..actions import Wait
from .base import Model, ScriptedModel

# --model name: default served model name on vLLM.
LOCAL_MODELS = {"qwen3-vl-8b": "Qwen/Qwen3-VL-8B-Instruct-FP8"}


def add_model_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("model")
    g.add_argument("--model", default="claude-haiku-5-5",
                   help=f"Claude model ID, a local model ({', '.join(LOCAL_MODELS)}), or 'scripted' for a no-model dry run")
    g.add_argument("--history-steps", type=int, default=8, help="recent steps shown to the model")
    g.add_argument("--goal", help="goal shown to the model every step, e.g. 'Start a singleplayer run'")
    g.add_argument("--prompt-file", help="replace the Spire system prompt, e.g. prompts/click_target.txt")
    g.add_argument("--max-tokens", type=int, help="output token cap (default: the adapter's own)")
    # Claude only.
    g.add_argument("--thinking", choices=["on", "off"], default="off", help="Claude only")
    g.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"], help="Claude only")
    g.add_argument("--send-width", type=int, help="Claude only: shrink screenshots to this width before sending, e.g. 960")
    # Local model only.
    g.add_argument("--base-url", default="http://localhost:8000/v1", help="local model: vLLM OpenAI-compatible endpoint")
    g.add_argument("--served-model", help="local model: name vLLM serves it under (default: the model's HF ID), or a LoRA adapter")
    g.add_argument("--temperature", type=float, help="local model: sampling temperature (default 0.7)")
    g.add_argument("--seed", type=int, help="local model: sampling seed")


def _prompt(args: argparse.Namespace) -> str | None:
    if not getattr(args, "prompt_file", None):
        return None
    with open(args.prompt_file, encoding="utf-8") as f:
        return f.read().strip()


def build_model(args: argparse.Namespace) -> Model:
    prompt = _prompt(args)
    if args.model == "scripted":
        return ScriptedModel([[Wait(0.1)]] * (getattr(args, "max_steps", None) or 1000))

    if args.model in LOCAL_MODELS:
        from .qwen import QwenConfig, QwenModel, adapt_prompt

        config = QwenConfig(
            base_url=args.base_url,
            served_model=args.served_model or LOCAL_MODELS[args.model],
            api_key=os.environ.get("VLLM_API_KEY") or None,
            history_steps=args.history_steps,
            goal=args.goal,
            seed=args.seed,
            coop=bool(getattr(args, "coop", False)),
        )
        if args.temperature is not None:
            config.temperature = args.temperature
        if args.max_tokens:
            config.max_tokens = args.max_tokens
        if prompt:
            config.system_prompt = adapt_prompt(prompt)
        return QwenModel(config)

    from .claude import ClaudeConfig, ClaudeModel

    config = ClaudeConfig(
        model=args.model, thinking=args.thinking, effort=args.effort, history_steps=args.history_steps,
        send_width=args.send_width, goal=args.goal, max_tokens=args.max_tokens,
        coop=bool(getattr(args, "coop", False)),
    )
    if prompt:
        config.system_prompt = prompt
    return ClaudeModel(config)
