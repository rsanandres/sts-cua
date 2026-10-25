"""Run the agent against an X display.

    python -m sts_cua --display :99 --model claude-haiku-5-5 --thinking off --max-steps 20
    python -m sts_cua --display :99 --model qwen3-vl-8b --max-steps 20     # local vLLM
    python -m sts_cua --display :99 --coop     # co-op with a human host; start it in the host's lobby
"""

from __future__ import annotations

import argparse
import json

from . import coop
from .loop import LoopConfig, run
from .models.factory import add_model_args, build_model
from .runlog import RunLog
from .screen import X11Screen

# Co-op runs as fast as possible: the human teammate waits on every slow step.
COOP_DEFAULTS = {"max_steps": coop.MAX_STEPS, "settle": coop.SETTLE_SECONDS, "history_steps": coop.HISTORY_STEPS}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sts_cua", description=__doc__.splitlines()[0])
    parser.add_argument("--display", default=":99", help="X display to capture and drive")
    parser.add_argument("--max-steps", type=int, default=20, help=f"step cap (co-op default {coop.MAX_STEPS})")
    parser.add_argument("--max-cost", type=float, default=1.0, help="stop when the run's API cost reaches this many USD")
    parser.add_argument("--settle", type=float, default=1.0,
                        help=f"seconds to wait after each step (co-op default {coop.SETTLE_SECONDS})")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--game-build", default="unknown", help="Spire 2 build, recorded in the run log")
    parser.add_argument("--state-url", default="http://localhost:15526/api/v1/singleplayer",
                        help="STS2MCP game state endpoint, logged each step as ground truth (never shown to the model)")
    parser.add_argument("--no-state", action="store_true", help="don't read game state from the mod")
    add_model_args(parser)
    parser.add_argument("--memory", metavar="PATH",
                        help="note store, e.g. memory/notes.jsonl: show matching lessons from earlier runs each step")
    g = parser.add_argument_group("co-op")
    g.add_argument("--coop", action="store_true",
                   help="play co-op with a human teammate who hosts; start once this game shows the host's lobby")
    g.add_argument("--coop-idle-minutes", type=float, default=coop.IDLE_MINUTES,
                   help="co-op: stop after this many minutes with no action of the agent's that changed the screen")
    g.add_argument("--no-rules", action="store_true", help="co-op: ask the model every step (no general rules)")
    args = parser.parse_args(argv)
    if args.coop:
        # Explicit flags still win: the co-op defaults only replace the solo ones.
        parser.set_defaults(**COOP_DEFAULTS)
        args = parser.parse_args(argv)
        if not args.goal:
            args.goal = coop.GOAL
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    screen = X11Screen(args.display)
    model = build_model(args)

    notes_fn, memory_meta = None, None
    if args.memory:
        from .memory.retrieve import for_run

        notes_fn, memory_meta = for_run(args.memory, args.game_build)

    log = RunLog(args.runs_dir, meta=run_meta(args, model.name, screen.size, memory_meta))
    state_fn = None
    if not args.no_state:
        from .state import fetch_state, summarize

        if args.coop:
            state_fn = lambda: coop.summarize(coop.fetch_state(args.state_url))  # noqa: E731
        else:
            state_fn = lambda: summarize(fetch_state(args.state_url))  # noqa: E731
    summary = run(model, screen, log, loop_config(args), state_fn=state_fn, notes_fn=notes_fn, **coop_hooks(args))
    print(json.dumps({"run_dir": str(log.dir), **summary}, indent=2))


def run_meta(args: argparse.Namespace, model_name: str, screen_size, memory_meta: dict | None) -> dict:
    return {
        "model": model_name,
        "display": args.display,
        "screen_size": list(screen_size),
        "game_build": args.game_build,
        "prompt_file": args.prompt_file,
        "send_width": args.send_width,
        "goal": args.goal,
        "max_steps": args.max_steps,
        "max_cost_usd": args.max_cost,
        **({"memory": memory_meta} if memory_meta else {}),
        # Co-op runs are not solo data: score/memory/supervisor leave them out (coop.is_coop).
        **({"mode": coop.MODE, "coop_idle_minutes": args.coop_idle_minutes, "rules": not args.no_rules}
           if args.coop else {}),
    }


def loop_config(args: argparse.Namespace) -> LoopConfig:
    config = LoopConfig(max_steps=args.max_steps, max_cost_usd=args.max_cost, settle_seconds=args.settle)
    if args.coop:
        config.max_idle_steps = coop.MAX_IDLE_STEPS
        config.max_idle_seconds = args.coop_idle_minutes * 60
    return config


def coop_hooks(args: argparse.Namespace) -> dict:
    """The loop hooks co-op adds: waiting for the teammate, the general rules, and stopping when the run ends."""
    if not args.coop:
        return {}
    from .rules import decide
    from .score import RunOverDetector

    return {"waiting_fn": coop.waiting, "stop_fn": RunOverDetector(),
            **({} if args.no_rules else {"rules_fn": decide})}


if __name__ == "__main__":
    main()
