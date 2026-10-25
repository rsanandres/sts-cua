"""Play full Spire 2 runs back to back under a daily API budget, and score each one.

    python -m sts_cua.supervisor --display :99 --model claude-haiku-5-5 --daily-budget 2.00

Before each run the game must be on the main menu with no saved run; if it isn't, a
short reset run (its own run dir, meta kind "reset") asks the model to get there. Each
game run stops when the mod state shows the run is over (score.RunOverDetector), then
gets a score.json and one line in runs/ledger.jsonl.

The daily budget is the sum of cost_usd in runs/*/summary.json whose ended_at falls on
today's date in America/Los_Angeles, so reset runs and runs from earlier sessions count.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .loop import LoopConfig, run
from .models.base import Model
from .models.factory import add_model_args
from .runlog import RunLog
from .score import RunOverDetector, at_main_menu, load_states, score_run
from .screen import Screen

BUDGET_TZ = "America/Los_Angeles"

GAME_GOAL = (
    "Start a new singleplayer run from the main menu and play it to the end. "
    "Choose Singleplayer, then a Standard run if asked, keep the default character and confirm. "
    "If asked whether to enable tutorials, choose No. Then play the whole run: fights, rewards, "
    "map, events, shops and rest sites, and try to win. Never choose Give Up or Save and Quit."
)
RESET_GOAL = (
    "Get the game back to the main menu with no run in progress. On a game over or victory screen, "
    "click the button that returns to the main menu. If a run is still in progress, press Escape to "
    "open the pause menu, choose Give Up and confirm. If the main menu shows Continue and Abandon Run, "
    "choose Abandon Run and confirm. The goal is done when the main menu shows Singleplayer and no Continue button."
)


@dataclass
class SupervisorConfig:
    runs_dir: Path = Path("runs")
    daily_budget_usd: float = 2.0
    reserve_usd: float = 0.10  # don't start a run with less than this left today
    max_cost_per_run: float | None = None  # None: whatever is left of today's budget
    max_runs: int | None = None
    max_steps: int = 3000
    max_idle_steps: int = 10
    reset_max_steps: int = 40
    settle_seconds: float = 1.0
    max_unstarted_runs: int = 3  # stop after this many runs in a row that never got into a run
    state_retries: int = 5  # polls before deciding the mod is unreachable
    state_retry_seconds: float = 5.0
    game_goal: str = GAME_GOAL
    game_build: str = "unknown"
    timezone: str = BUDGET_TZ
    meta: dict = field(default_factory=dict)  # extra fields for every run's meta.json


@dataclass
class Session:
    runs: list[dict]  # the ledger lines written this session
    stop_reason: str


def spent_on(runs_dir: str | Path, day: date, tz: str = BUDGET_TZ) -> float:
    """API cost of runs in runs_dir that ended on `day` in time zone tz."""
    zone = ZoneInfo(tz)
    total = 0.0
    for path in Path(runs_dir).glob("*/summary.json"):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            ended = datetime.fromisoformat(summary["ended_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)  # RunLog writes UTC
        if ended.astimezone(zone).date() == day:
            total += float(summary.get("cost_usd") or 0.0)
    return total


def spent_today(runs_dir: str | Path, now: datetime, tz: str = BUDGET_TZ) -> float:
    return spent_on(runs_dir, now.astimezone(ZoneInfo(tz)).date(), tz)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def supervise(
    config: SupervisorConfig,
    model_factory: Callable[[str], Model],
    screen_factory: Callable[[], Screen],
    state_fn: Callable[[], dict | None],
    clock: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> Session:
    """Run games until the budget, the run limit, or a problem stops the session.

    model_factory(goal) builds a model for one run; state_fn returns the summarized mod state.
    """
    runs_dir = Path(config.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    unstarted = 0

    def remaining() -> float:
        return config.daily_budget_usd - spent_today(runs_dir, clock(), config.timezone)

    def wait_for_state() -> dict | None:
        for attempt in range(config.state_retries):
            state = state_fn()
            if state is not None:
                return state
            if attempt + 1 < config.state_retries:
                sleep(config.state_retry_seconds)
        return None

    def loop_config(max_steps: int, budget_left: float) -> LoopConfig:
        cap = budget_left if config.max_cost_per_run is None else min(config.max_cost_per_run, budget_left)
        return LoopConfig(
            max_steps=max_steps, max_cost_usd=cap, settle_seconds=config.settle_seconds,
            max_idle_steps=config.max_idle_steps,
        )

    def meta(model: Model, kind: str, goal: str, screen: Screen) -> dict:
        return {
            **config.meta, "kind": kind, "model": model.name, "goal": goal, "screen_size": list(screen.size),
            "game_build": config.game_build,
        }

    while True:
        if config.max_runs is not None and len(entries) >= config.max_runs:
            return Session(entries, "max_runs")
        if remaining() < config.reserve_usd:
            return Session(entries, "budget")

        state = wait_for_state()
        if state is None:
            return Session(entries, "no_state")
        if not at_main_menu(state):
            echo(f"not at the main menu ({state.get('state_type')}); resetting")
            model, screen = model_factory(RESET_GOAL), screen_factory()
            log = RunLog(runs_dir, meta=meta(model, "reset", RESET_GOAL, screen))
            run(model, screen, log, loop_config(config.reset_max_steps, remaining()), state_fn=state_fn,
                stop_fn=lambda s: "at_main_menu" if at_main_menu(s) else None)
            if not at_main_menu(wait_for_state()):
                return Session(entries, "not_at_main_menu")
            if remaining() < config.reserve_usd:
                return Session(entries, "budget")

        model, screen = model_factory(config.game_goal), screen_factory()
        log = RunLog(runs_dir, meta=meta(model, "game", config.game_goal, screen))
        started_at = json.loads((log.dir / "meta.json").read_text(encoding="utf-8"))["started_at"]
        summary = run(model, screen, log, loop_config(config.max_steps, remaining()), state_fn=state_fn,
                      stop_fn=RunOverDetector())

        result = score_run(load_states(log.dir))
        (log.dir / "score.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        entry = {
            "run_dir": str(log.dir), "started_at": started_at, "ended_at": summary["ended_at"],
            "model": model.name, "cost_usd": summary["cost_usd"], "steps": summary["steps"],
            "end_reason": summary["end_reason"], "outcome": result.outcome, "floor": result.max_floor,
            "bosses_killed": result.bosses_killed, "score": result.score, "character": result.character,
            "ascension": result.ascension, "game_build": config.game_build,
        }
        with (runs_dir / "ledger.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        entries.append(entry)
        echo(json.dumps(entry))

        unstarted = 0 if result.started else unstarted + 1
        if unstarted >= config.max_unstarted_runs:
            return Session(entries, "runs_not_starting")


def build_model(args: argparse.Namespace, goal: str) -> Model:
    """The model from the shared --model flags, with this run's goal (game or reset)."""
    from .models.factory import build_model as from_flags

    return from_flags(argparse.Namespace(**{**vars(args), "goal": goal}))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sts_cua.supervisor", description=__doc__.splitlines()[0])
    parser.add_argument("--display", default=":99", help="X display to capture and drive")
    parser.add_argument("--daily-budget", type=float, default=2.0, help="USD per day (America/Los_Angeles), counted from runs/*/summary.json")
    parser.add_argument("--reserve", type=float, default=0.10, help="don't start a run with less than this many USD left today")
    parser.add_argument("--max-cost", type=float, help="USD cap per run (default: what is left of today's budget)")
    parser.add_argument("--max-runs", type=int, help="stop after this many game runs")
    parser.add_argument("--max-steps", type=int, default=3000, help="step cap per run")
    parser.add_argument("--max-idle-steps", type=int, default=10, help="end a run after this many steps in a row with no action")
    parser.add_argument("--reset-max-steps", type=int, default=40, help="step cap for getting back to the main menu")
    parser.add_argument("--settle", type=float, default=1.0, help="seconds to wait after each step")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--game-build", default="unknown", help="Spire 2 build, recorded in each run log and the ledger")
    parser.add_argument("--state-url", default="http://localhost:15526/api/v1/singleplayer", help="STS2MCP game state endpoint")
    add_model_args(parser)  # --model, --goal (default: GAME_GOAL), --prompt-file, ...
    args = parser.parse_args(argv)

    from .screen import X11Screen
    from .state import fetch_state, summarize

    config = SupervisorConfig(
        runs_dir=Path(args.runs_dir), daily_budget_usd=args.daily_budget, reserve_usd=args.reserve,
        max_cost_per_run=args.max_cost, max_runs=args.max_runs, max_steps=args.max_steps,
        max_idle_steps=args.max_idle_steps, reset_max_steps=args.reset_max_steps, settle_seconds=args.settle,
        game_goal=args.goal or GAME_GOAL, game_build=args.game_build,
        meta={"display": args.display, "prompt_file": args.prompt_file, "send_width": args.send_width},
    )
    session = supervise(
        config,
        model_factory=lambda goal: build_model(args, goal),
        screen_factory=lambda: X11Screen(args.display),
        state_fn=lambda: summarize(fetch_state(args.state_url)),
    )
    print(json.dumps({"stop_reason": session.stop_reason, "runs": len(session.runs)}, indent=2))


if __name__ == "__main__":
    main()
