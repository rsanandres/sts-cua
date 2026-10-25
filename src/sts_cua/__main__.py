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
    args = parser.parse_args(argv)

    screen = X11Screen(args.display)
    if args.model == "scripted":
        model = ScriptedModel([[Wait(0.1)]] * args.max_steps)
    else:
        from .models.claude import ClaudeConfig, ClaudeModel

        model = ClaudeModel(ClaudeConfig(
            model=args.model, thinking=args.thinking, effort=args.effort, history_steps=args.history_steps,
        ))

    log = RunLog(args.runs_dir, meta={
        "model": model.name,
        "display": args.display,
        "screen_size": list(screen.size),
        "game_build": args.game_build,
        "max_steps": args.max_steps,
        "max_cost_usd": args.max_cost,
    })
    summary = run(model, screen, log, LoopConfig(
        max_steps=args.max_steps, max_cost_usd=args.max_cost, settle_seconds=args.settle,
    ))
    print(json.dumps({"run_dir": str(log.dir), **summary}, indent=2))


if __name__ == "__main__":
    main()
