"""Run the agent against an X display.

    python -m sts_cua --display :99 --model claude-haiku-5-5 --thinking off --max-steps 20
"""

from __future__ import annotations

import argparse
import json

from .actions import Wait
from .loop import LoopConfig, run
from .models.base import ScriptedModel
from .runlog import RunLog
from .screen import X11Screen


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sts_cua", description=__doc__.splitlines()[0])
    parser.add_argument("--display", default=":99", help="X display to capture and drive")
    parser.add_argument("--model", default="claude-haiku-5-5", help="Claude model ID, or 'scripted' for a no-model dry run")
    parser.add_argument("--thinking", choices=["on", "off"], default="off")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--history-steps", type=int, default=8)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-cost", type=float, default=1.0, help="stop when the run's API cost reaches this many USD")
    parser.add_argument("--settle", type=float, default=1.0, help="seconds to wait after each step")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--game-build", default="unknown", help="Spire 2 build, recorded in the run log")
    parser.add_argument("--goal", help="goal shown to the model every step, e.g. 'Start a singleplayer run'")
    parser.add_argument("--send-width", type=int, help="shrink screenshots to this width before sending to Claude, e.g. 960")
    parser.add_argument("--state-url", default="http://localhost:15526/api/v1/singleplayer",
                        help="STS2MCP game state endpoint, logged each step as ground truth (never shown to the model)")
    parser.add_argument("--no-state", action="store_true", help="don't read game state from the mod")
    parser.add_argument("--prompt-file", help="replace the Spire system prompt, e.g. prompts/click_target.txt")
    args = parser.parse_args(argv)

    screen = X11Screen(args.display)
    if args.model == "scripted":
        model = ScriptedModel([[Wait(0.1)]] * args.max_steps)
    else:
        from .models.claude import ClaudeConfig, ClaudeModel

        config = ClaudeConfig(
            model=args.model, thinking=args.thinking, effort=args.effort, history_steps=args.history_steps,
            send_width=args.send_width, goal=args.goal,
        )
        if args.prompt_file:
            with open(args.prompt_file, encoding="utf-8") as f:
                config.system_prompt = f.read().strip()
        model = ClaudeModel(config)

    log = RunLog(args.runs_dir, meta={
        "model": model.name,
        "display": args.display,
        "screen_size": list(screen.size),
        "game_build": args.game_build,
        "prompt_file": args.prompt_file,
        "send_width": args.send_width,
        "goal": args.goal,
        "max_steps": args.max_steps,
        "max_cost_usd": args.max_cost,
    })
    state_fn = None
    if not args.no_state:
        from .state import fetch_state, summarize

        state_fn = lambda: summarize(fetch_state(args.state_url))  # noqa: E731
    summary = run(model, screen, log, LoopConfig(
        max_steps=args.max_steps, max_cost_usd=args.max_cost, settle_seconds=args.settle,
    ), state_fn=state_fn)
    print(json.dumps({"run_dir": str(log.dir), **summary}, indent=2))


if __name__ == "__main__":
    main()
