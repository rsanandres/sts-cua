"""Run the agent against an X display.

    python -m sts_cua --display :99 --model claude-haiku-5-5 --thinking off --max-steps 20
    python -m sts_cua --display :99 --model qwen3-vl-8b --max-steps 20     # local vLLM
"""

from __future__ import annotations

import argparse
import json

from .loop import LoopConfig, run
from .models.factory import add_model_args, build_model
from .runlog import RunLog
from .screen import X11Screen


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sts_cua", description=__doc__.splitlines()[0])
    parser.add_argument("--display", default=":99", help="X display to capture and drive")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-cost", type=float, default=1.0, help="stop when the run's API cost reaches this many USD")
    parser.add_argument("--settle", type=float, default=1.0, help="seconds to wait after each step")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--game-build", default="unknown", help="Spire 2 build, recorded in the run log")
    parser.add_argument("--state-url", default="http://localhost:15526/api/v1/singleplayer",
                        help="STS2MCP game state endpoint, logged each step as ground truth (never shown to the model)")
    parser.add_argument("--no-state", action="store_true", help="don't read game state from the mod")
    add_model_args(parser)
    args = parser.parse_args(argv)

    screen = X11Screen(args.display)
    model = build_model(args)

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
