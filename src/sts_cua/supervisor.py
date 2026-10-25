"""Play full Spire 2 runs back to back under a daily API budget, and score each one.

    python -m sts_cua.supervisor --display :99 --model claude-haiku-5-5 --daily-budget 2.00

Solo only: the session stops ("coop_session") when the mod shows a co-op lobby or run, and
co-op runs (meta mode "coop", from python -m sts_cua --coop) are never scored here.

Before each run the game must be on the main menu with no saved run; if it isn't, a
short reset run (its own run dir, meta kind "reset") asks the model to get there. Each
game run stops when the mod state shows the run is over (score.RunOverDetector), then
gets a score.json and one line in runs/ledger.jsonl.

The outcome, floor and seed come from the game's own run history when the run is found
there (sts_cua.history: the mod's /api/v1/compendium, snapshotted before each game run so
the new entry is known exactly); otherwise from the logged states (score.score_run).

With --memory, each game run sees matching notes from the store (a fresh probation seed
per run, in meta.json["memory"]); after it, the run is scored against the store and one
reflect call (sts_cua.memory.reflect) edits it. Reset runs get no notes and no reflect.
SupervisorConfig.memory_frozen (used by sts_cua.eval) shows the notes but skips scoring
and reflect, so a frozen snapshot never changes.

The daily budget is the sum of cost_usd in runs/*/summary.json whose ended_at falls on
today's date in America/Los_Angeles, plus the cost_usd of runs/*/memory_reflect.json by
its time, so reset runs, reflect calls and runs from earlier sessions count.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .coop import in_session as in_coop_session
from .history import DEFAULT_URL as HISTORY_URL
from .history import HistoryEntry, entry_ids, fetch_history, find_run, run_outcome
from .loop import LoopConfig, run
from .memory.reflect import REFLECT_FILE, reflect
from .memory.retrieve import for_run
from .memory.runs import load_run
from .memory.score import score_run as score_notes
from .memory.store import NoteStore, now
from .models.base import Model
from .models.factory import add_model_args
from .runlog import RunLog
from .score import RunOverDetector, at_main_menu, load_states, score_run
from .screen import Screen

BUDGET_TZ = "America/Los_Angeles"
MEMORY_ERROR_FILE = "memory_error.json"

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
    memory_path: Path | None = None  # note store; None: no memory
    reflect: bool = True  # with memory: one reflect call after each game run
    reflect_reserve_usd: float = 0.05  # expected reflect cost: kept out of a run's cap, and needed to reflect
    memory_frozen: bool = False  # with memory: show notes but never score or reflect (the store stays unchanged)
    history_retries: int = 3  # run-history polls after a run that ended on the game-over screen or a menu
    history_retry_seconds: float = 2.0


@dataclass
class Session:
    runs: list[dict]  # the ledger lines written this session
    stop_reason: str


def spent_on(runs_dir: str | Path, day: date, tz: str = BUDGET_TZ) -> float:
    """API cost in runs_dir on `day` in time zone tz: runs by the time they ended, reflect calls by their time.

    summary.json holds only the run's own model calls and memory_reflect.json only the reflect call,
    so each dollar is counted once.
    """
    zone = ZoneInfo(tz)
    total = 0.0
    for name, stamp in (("summary.json", "ended_at"), (REFLECT_FILE, "time")):
        for path in Path(runs_dir).glob(f"*/{name}"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                at = datetime.fromisoformat(record[stamp])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)  # RunLog and the note store write UTC
            if at.astimezone(zone).date() == day:
                total += float(record.get("cost_usd") or 0.0)
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
    reflect_client_factory: Callable[[], object] | None = None,
    history_fn: Callable[[], list[HistoryEntry] | None] | None = None,
) -> Session:
    """Run games until the budget, the run limit, or a problem stops the session.

    model_factory(goal) builds a model for one run; state_fn returns the summarized mod state.
    reflect_client_factory() builds the Anthropic client for a reflect call (default: anthropic.Anthropic()).
    history_fn() returns the mod's run history (history.fetch_history), None when unreachable;
    without it, outcomes come from the states alone.
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

    learning = config.memory_path is not None and not config.memory_frozen
    reflecting = learning and config.reflect

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

    def learn(run_dir: Path) -> dict:
        """Score the run against the store, then reflect if wanted and affordable. Never raises."""
        out = {"notes_added": None, "notes_changed": None, "reflect_cost_usd": None}
        stage = "score"
        try:
            store, finished = NoteStore.load(config.memory_path), load_run(run_dir)
            score_notes(store, finished)
            store.save()
            if not config.reflect:
                return out
            if remaining() < config.reflect_reserve_usd:
                echo(f"skipping reflect for {run_dir.name}: under ${config.reflect_reserve_usd:.2f} left today")
                return out
            stage = "reflect"
            client = reflect_client_factory() if reflect_client_factory else None
            record = reflect(store, finished, client=client)
            results = [r.get("result", "") for r in record.get("results", [])]
            out.update(
                notes_added=sum(r == "added" for r in results),
                notes_changed=sum(r.startswith(("updated", "downvoted", "retired")) for r in results),
                reflect_cost_usd=record.get("cost_usd"),
            )
            if record.get("error"):
                echo(f"reflect for {run_dir.name}: {record['error']}")
        except Exception as exc:  # memory must never stop the session
            echo(f"memory {stage} failed for {run_dir.name}: {exc!r}")
            (run_dir / MEMORY_ERROR_FILE).write_text(json.dumps({
                "stage": stage, "time": now(), "error": repr(exc), "traceback": traceback.format_exc(),
            }, indent=2) + "\n", encoding="utf-8")
        return out

    def look_up(seen: set[str] | None, started_at: str, result, run_ended: bool) -> tuple[HistoryEntry | None, str | None]:
        """This run's new history entry, polling a few times when the run reached its end. Never raises."""
        if history_fn is None:
            return None, None
        tries = config.history_retries if run_ended else 1
        for attempt in range(max(tries, 1)):
            try:
                entries = history_fn()
                found = find_run(entries, datetime.fromisoformat(started_at), clock(), seen_ids=seen,
                                 character=result.character, floor=result.max_floor)
            except Exception as exc:  # the history is a bonus; the states still score the run
                echo(f"run history lookup failed: {exc!r}")
                return None, None
            if found is not None:
                return found, "new_id" if seen is not None else "time_window"
            if attempt + 1 < tries:
                sleep(config.history_retry_seconds)
        return None, None

    while True:
        if config.max_runs is not None and len(entries) >= config.max_runs:
            return Session(entries, "max_runs")
        if remaining() < config.reserve_usd:
            return Session(entries, "budget")

        state = wait_for_state()
        if state is None:
            return Session(entries, "no_state")
        if in_coop_session(state):
            # Solo only: never reset, or play in, a co-op lobby or run (python -m sts_cua --coop does co-op).
            return Session(entries, "coop_session")
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

        notes_fn, memory_meta = None, None
        if config.memory_path is not None:
            notes_fn, memory_meta = for_run(config.memory_path, config.game_build)
        seen = None
        if history_fn is not None:
            try:
                seen = entry_ids(history_fn())
            except Exception as exc:
                echo(f"run history snapshot failed: {exc!r}")
        model, screen = model_factory(config.game_goal), screen_factory()
        log = RunLog(runs_dir, meta={
            **meta(model, "game", config.game_goal, screen), **({"memory": memory_meta} if memory_meta else {}),
        })
        started_at = json.loads((log.dir / "meta.json").read_text(encoding="utf-8"))["started_at"]
        budget_left = remaining() - (config.reflect_reserve_usd if reflecting else 0.0)
        summary = run(model, screen, log, loop_config(config.max_steps, budget_left), state_fn=state_fn,
                      stop_fn=RunOverDetector(), notes_fn=notes_fn)

        result = score_run(load_states(log.dir))
        found, match = look_up(seen, started_at, result, summary["end_reason"].startswith("run_over"))
        scored = run_outcome(result, found, match)
        if scored["outcome_states"] is not None:
            echo(f"{log.dir.name}: run history says {scored['outcome']}, the states said {scored['outcome_states']}")
        (log.dir / "score.json").write_text(json.dumps(scored, indent=2) + "\n", encoding="utf-8")
        entry = {
            "run_dir": str(log.dir), "started_at": started_at, "ended_at": summary["ended_at"],
            "model": model.name, "cost_usd": summary["cost_usd"], "steps": summary["steps"],
            "end_reason": summary["end_reason"], "outcome": scored["outcome"], "floor": scored["max_floor"],
            "bosses_killed": result.bosses_killed, "score": scored["score"], "character": result.character,
            "ascension": scored["ascension"], "seed": scored["seed"], "outcome_source": scored["outcome_source"],
            "outcome_states": scored["outcome_states"], "game_build": config.game_build,
            "memory": str(config.memory_path) if config.memory_path is not None else None,
            "notes_added": None, "notes_changed": None, "reflect_cost_usd": None,
        }
        if learning:
            entry.update(learn(log.dir))
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
    parser.add_argument("--compendium-url", default=HISTORY_URL,
                        help="STS2MCP run history endpoint, the source of each run's outcome and seed")
    parser.add_argument("--memory", metavar="PATH",
                        help="note store, e.g. memory/notes.jsonl: show matching notes in game runs, then score and reflect")
    parser.add_argument("--no-reflect", action="store_true", help="with --memory: score runs but make no reflect call")
    parser.add_argument("--reflect-reserve", type=float, default=0.05,
                        help="expected USD per reflect call: kept back from each game run, and skip reflect with less left today")
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
        memory_path=Path(args.memory) if args.memory else None, reflect=not args.no_reflect,
        reflect_reserve_usd=args.reflect_reserve,
    )
    session = supervise(
        config,
        model_factory=lambda goal: build_model(args, goal),
        screen_factory=lambda: X11Screen(args.display),
        state_fn=lambda: summarize(fetch_state(args.state_url)),
        history_fn=lambda: fetch_history(args.compendium_url),
    )
    print(json.dumps({"stop_reason": session.stop_reason, "runs": len(session.runs)}, indent=2))


if __name__ == "__main__":
    main()
