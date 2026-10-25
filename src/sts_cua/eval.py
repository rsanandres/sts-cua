"""Memory A/B evaluation: no memory vs a frozen note snapshot, on fixed seeds when the game allows it.

    python -m sts_cua.eval snapshot                                   # memory/notes.jsonl -> memory/frozen/notes-<date>.jsonl
    python -m sts_cua.eval plan ab1 --arms none frozen:memory/frozen/notes-20261025.jsonl \\
        --seeds seeds/eval.txt --model claude-haiku-5-5 --daily-budget 2 --max-cost 0.5
    python -m sts_cua.eval run ab1 --display :99                      # plays the plan; run again to resume
    python -m sts_cua.eval report ab1                                 # per-arm stats and the test
    python -m sts_cua.eval list

docs/learning.md's plan: same character, A0, primary metric the run score (100 for a win,
else floor + 52/3 per boss), win rate secondary, evaluation seeds kept apart from the runs
memory was written on.

- Paired (a seed file is given): each seed is played once per arm, as a Spire 2 Custom run
  with that seed typed in by the agent (the goal prompt says how). The supervisor reads
  each run's seed from the game's run history (sts_cua.history, the mod's read-only
  GET /api/v1/compendium), so a run on the wrong seed is caught and retried. Test: Wilcoxon
  signed-rank on the per-seed score difference.
- Unpaired (no seeds): standard runs, arms interleaved in time. Test: Mann-Whitney U.
  The report says the comparison is unpaired.

Arm order alternates (AB, BA, AB, ...) so neither arm always plays first. Every run goes
through supervisor.supervise (one game run per call) into the shared runs/ directory, so
the daily budget, reset runs, scoring and the ledger work as for any session. A frozen arm
shows the snapshot's notes but never scores or reflects (SupervisorConfig.memory_frozen),
and the snapshot's sha256 is checked before and after every run. The no-memory arm
writes nothing either.

Files in runs/evals/<name>/: plan.json, results.jsonl (one line per finished game run,
valid or not), report.json (rewritten after each run). A run is valid when it got into a
run, on the planned seed (when it could be read), ascension and character; an invalid slot
is retried up to max_attempts times. `run` skips slots already done, so a session that
stopped on the budget picks up where it left off.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import stats
from .history import DEFAULT_URL as COMPENDIUM_URL
from .history import HistoryEntry, character_key, fetch_history
from .models.base import Model
from .models.factory import add_model_args
from .screen import Screen
from .supervisor import BUDGET_TZ, SupervisorConfig, supervise

EVALS_DIR = "evals"  # under the runs directory
NAME_RE = re.compile(r"^[\w.-]+$")
SEED_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")
DEFAULT_STORE = Path("memory/notes.jsonl")
SNAPSHOT_DIR = Path("memory/frozen")
PLAN_FILE, RESULTS_FILE, REPORT_FILE = "plan.json", "results.jsonl", "report.json"

_PLAY = (
    "If asked whether to enable tutorials, choose No. Then play the whole run: fights, rewards, map, events, "
    "shops and rest sites, and try to win. Never choose Give Up or Save and Quit."
)
SEEDED_GOAL = (
    "Start a new seeded Custom run from the main menu and play it to the end. Choose Singleplayer, then Custom. "
    "On the custom run screen choose {character}, set Ascension to {ascension}, leave every modifier off, click the "
    "seed field, delete anything in it and type the seed {seed} exactly, then start the run. " + _PLAY
)
STANDARD_GOAL = (
    "Start a new singleplayer run from the main menu and play it to the end. Choose Singleplayer, then a Standard "
    "run if asked, choose {character}, set Ascension to {ascension} if an ascension selector is shown, and confirm. "
    + _PLAY
)


# ---- arms, seeds and the plan

@dataclass
class Arm:
    label: str  # "none" or "frozen:<path>"
    memory: Path | None


def parse_arm(text: str) -> Arm:
    if text == "none":
        return Arm("none", None)
    if text.startswith("frozen:") and len(text) > len("frozen:"):
        return Arm(text, Path(text[len("frozen:"):]))
    raise ValueError(f"an arm is 'none' or 'frozen:<snapshot path>', not {text!r}")


def normalize_seed(seed: str) -> str:
    return seed.strip().upper()


def read_seeds(path: str | Path) -> list[str]:
    """One seed per line; blank lines and # comments are skipped. Seeds are letters and digits."""
    seeds = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if not SEED_RE.match(line):
            raise ValueError(f"{path}:{number}: a seed is 1-32 letters and digits, not {line!r}")
        seed = normalize_seed(line)
        if seed in seeds:
            raise ValueError(f"{path}:{number}: seed {seed} is listed twice")
        seeds.append(seed)
    return seeds


def training_seeds(runs_dir: str | Path) -> list[str]:
    """Seeds of the ledger's game runs that played with memory and could write it (not evaluation runs)."""
    path = Path(runs_dir) / "ledger.jsonl"
    seeds: list[str] = []
    if not path.exists():
        return seeds
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not entry.get("memory") or not entry.get("seed"):
            continue
        try:
            meta = json.loads((Path(runs_dir) / Path(entry["run_dir"]).name / "meta.json").read_text(encoding="utf-8"))
        except (ValueError, KeyError, TypeError, OSError):
            meta = {}
        if "eval" not in meta:
            seed = normalize_seed(str(entry["seed"]))
            if seed not in seeds:
                seeds.append(seed)
    return seeds


def schedule(arms: list[str], seeds: list[str] | None, runs_per_arm: int) -> list[dict]:
    """The run order: pair k plays both arms, in order AB for even k and BA for odd k.

    Paired, pair k is seeds[k]; unpaired, a pair is just two runs next to each other in time.
    """
    slots = []
    for k in range(runs_per_arm):
        order = arms if k % 2 == 0 else list(reversed(arms))
        for arm in order:
            slots.append({"slot": len(slots), "pair": k, "arm": arm, "seed": seeds[k] if seeds else None})
    return slots


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def make_plan(
    name: str,
    arms: list[str],
    model: dict,
    seeds: list[str] | None = None,
    runs_per_arm: int | None = None,
    training_seeds: list[str] | None = None,
    character: str = "the Ironclad",
    ascension: int = 0,
    game_build: str = "unknown",
    daily_budget_usd: float = 2.0,
    reserve_usd: float = 0.10,
    max_cost_per_run: float | None = 0.5,
    max_steps: int = 3000,
    max_idle_steps: int = 10,
    max_attempts: int = 2,
    clock: Callable[[], datetime] = _now,
) -> dict:
    if not NAME_RE.match(name) or name in (".", ".."):
        raise ValueError(f"name must match {NAME_RE.pattern}")
    parsed = [parse_arm(a) for a in arms]
    if len(parsed) != 2 or parsed[0].label == parsed[1].label:
        raise ValueError("give exactly two different arms, e.g. none frozen:memory/frozen/notes-20261025.jsonl")
    for arm in parsed:
        if arm.memory is not None and not arm.memory.is_file():
            raise ValueError(f"no snapshot at {arm.memory}; make one with `python -m sts_cua.eval snapshot`")
    if seeds:
        overlap = sorted(set(seeds) & {normalize_seed(s) for s in training_seeds or []})
        if overlap:
            raise ValueError(f"evaluation seeds also used for training: {', '.join(overlap)}")
        runs_per_arm = len(seeds) if runs_per_arm is None else runs_per_arm
        if not 1 <= runs_per_arm <= len(seeds):
            raise ValueError(f"runs per arm must be 1..{len(seeds)} (each seed is played once per arm)")
        seeds = seeds[:runs_per_arm]
    elif runs_per_arm is None or runs_per_arm < 1:
        raise ValueError("without seeds, give --runs-per-arm")
    if max_attempts < 1:
        raise ValueError("max attempts must be at least 1")
    return {
        "name": name,
        "created_at": clock().isoformat(),
        "mode": "paired" if seeds else "unpaired",
        "arms": [{"label": a.label, "memory": str(a.memory) if a.memory else None,
                  "sha256": sha256(a.memory) if a.memory else None} for a in parsed],
        "seeds": seeds or [],
        "runs_per_arm": runs_per_arm,
        "character": character,
        "ascension": ascension,
        "game_build": game_build,
        "model": model,
        "budget": {"daily_budget_usd": daily_budget_usd, "reserve_usd": reserve_usd,
                   "max_cost_per_run": max_cost_per_run},
        "max_steps": max_steps,
        "max_idle_steps": max_idle_steps,
        "max_attempts": max_attempts,
        "schedule": schedule([a.label for a in parsed], seeds, runs_per_arm),
    }


def eval_dir(runs_dir: str | Path, name: str) -> Path:
    if not NAME_RE.match(name) or name in (".", ".."):
        raise ValueError(f"name must match {NAME_RE.pattern}")
    return Path(runs_dir) / EVALS_DIR / name


def write_plan(runs_dir: str | Path, plan: dict) -> Path:
    out = eval_dir(runs_dir, plan["name"])
    if (out / PLAN_FILE).exists():
        raise FileExistsError(f"{out / PLAN_FILE} exists; pick another name")
    out.mkdir(parents=True, exist_ok=True)
    (out / PLAN_FILE).write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return out


def load_plan(directory: str | Path) -> dict:
    return json.loads((Path(directory) / PLAN_FILE).read_text(encoding="utf-8"))


def load_results(directory: str | Path) -> list[dict]:
    path = Path(directory) / RESULTS_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a line cut off by a crash
    return out


def goal_for(plan: dict, slot: dict) -> str:
    template = SEEDED_GOAL if slot.get("seed") else STANDARD_GOAL
    return template.format(character=plan["character"], ascension=plan["ascension"], seed=slot.get("seed"))


# ---- running

def judge(plan: dict, slot: dict, entry: dict, seed_played: str | None) -> tuple[bool, str | None, list[str]]:
    """(valid, why not, warnings) for one finished game run."""
    warnings = []
    if entry.get("floor") is None:
        return False, "not_started", warnings
    if slot.get("seed"):
        if seed_played is None:
            warnings.append("seed_unverified")
        elif normalize_seed(seed_played) != slot["seed"]:
            return False, "seed_mismatch", warnings
    if entry.get("ascension") is not None and entry["ascension"] != plan["ascension"]:
        return False, "wrong_ascension", warnings
    if entry.get("character") and character_key(entry["character"]) != character_key(plan["character"]):
        return False, "wrong_character", warnings
    return True, None, warnings


@dataclass
class EvalSession:
    stop_reason: str  # complete, budget, max_runs, snapshot_changed, runs_not_starting, or a supervisor reason
    runs: list[dict]  # results.jsonl lines written this session


def run_eval(
    directory: str | Path,
    model_factory: Callable[[str], Model],
    screen_factory: Callable[[], Screen],
    state_fn: Callable[[], dict | None],
    history_fn: Callable[[], list[HistoryEntry] | None] | None = None,
    runs_dir: str | Path = "runs",
    max_runs: int | None = None,
    daily_budget_usd: float | None = None,
    settle_seconds: float = 1.0,
    state_retry_seconds: float = 5.0,
    meta: dict | None = None,
    clock: Callable[[], datetime] = _now,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
    max_not_started: int = 3,
) -> EvalSession:
    """Play the plan's remaining slots in order, one supervise() call per game run."""
    directory = Path(directory)
    plan = load_plan(directory)
    results = load_results(directory)
    budget = plan["budget"]
    cap = budget.get("max_cost_per_run")
    # Never start a run that the daily budget would cut short: keep a whole run's cap in reserve.
    reserve = max(budget.get("reserve_usd") or 0.0, cap or 0.0)
    arms = {a["label"]: a for a in plan["arms"]}
    written: list[dict] = []
    not_started = 0

    def finish(reason: str) -> EvalSession:
        write_report(directory, plan, load_results(directory), stop_reason=reason)
        return EvalSession(reason, written)

    def snapshot_ok(arm: dict) -> bool:
        if arm["memory"] is None:
            return True
        try:
            return sha256(arm["memory"]) == arm["sha256"]
        except OSError:
            return False

    for slot in plan["schedule"]:
        mine = [r for r in results if r["slot"] == slot["slot"]]
        attempts = len(mine)
        if any(r["valid"] for r in mine):
            continue
        arm = arms[slot["arm"]]
        while attempts < plan["max_attempts"]:
            if max_runs is not None and len(written) >= max_runs:
                return finish("max_runs")
            if not snapshot_ok(arm):
                echo(f"snapshot {arm['memory']} is missing or changed since the plan was made")
                return finish("snapshot_changed")
            goal = goal_for(plan, slot)
            config = SupervisorConfig(
                runs_dir=Path(runs_dir), daily_budget_usd=daily_budget_usd or budget["daily_budget_usd"],
                reserve_usd=reserve, max_cost_per_run=cap, max_runs=1, max_steps=plan["max_steps"],
                max_idle_steps=plan["max_idle_steps"], settle_seconds=settle_seconds,
                state_retry_seconds=state_retry_seconds, game_goal=goal, game_build=plan["game_build"],
                meta={**(meta or {}), "eval": {"name": plan["name"], "slot": slot["slot"], "arm": slot["arm"],
                                               "seed": slot["seed"], "attempt": attempts + 1}},
                memory_path=Path(arm["memory"]) if arm["memory"] else None, memory_frozen=True, reflect=False,
            )
            session = supervise(config, model_factory, screen_factory, state_fn, clock=clock, sleep=sleep, echo=echo,
                                history_fn=history_fn)
            if not session.runs:
                return finish(session.stop_reason)
            entry = session.runs[0]
            seed_played = entry.get("seed")  # from the run history; None when the run wasn't found there
            valid, reason, warnings = judge(plan, slot, entry, seed_played)
            attempts += 1
            record = {
                **slot, "attempt": attempts, "time": clock().isoformat(), "valid": valid, "invalid_reason": reason,
                "warnings": warnings, "seed_played": seed_played,
                **{k: entry.get(k) for k in ("run_dir", "started_at", "ended_at", "model", "cost_usd", "steps",
                                             "end_reason", "outcome", "outcome_source", "floor", "bosses_killed", "score",
                                             "character", "ascension", "game_build")},
            }
            with (directory / RESULTS_FILE).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            written.append(record)
            results.append(record)
            write_report(directory, plan, results, stop_reason="running")
            echo(json.dumps({"eval": plan["name"], **{k: record[k] for k in (
                "slot", "arm", "seed", "attempt", "valid", "invalid_reason", "score", "cost_usd")}}))
            if not snapshot_ok(arm):
                echo(f"snapshot {arm['memory']} changed during slot {slot['slot']}")
                return finish("snapshot_changed")
            if valid:
                not_started = 0
                break
            not_started = not_started + 1 if reason == "not_started" else 0
            if not_started >= max_not_started:
                return finish("runs_not_starting")
    return finish("complete")


# ---- the report

def _mean(xs: list[float]) -> float | None:
    return round(statistics.fmean(xs), 4) if xs else None


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 4) if xs else None


def _arm_stats(label: str, valid: list[dict], everything: list[dict]) -> dict:
    scores = [float(r["score"]) for r in valid]
    floors = [float(r["floor"]) for r in valid if r.get("floor") is not None]
    wins = sum(r.get("outcome") == "won" for r in valid)
    cost = sum(float(r.get("cost_usd") or 0.0) for r in everything)
    end_reasons: dict[str, int] = {}
    for r in valid:
        end_reasons[str(r.get("end_reason"))] = end_reasons.get(str(r.get("end_reason")), 0) + 1
    return {
        "arm": label, "runs": len(valid), "invalid_runs": len(everything) - len(valid),
        "mean_score": _mean(scores), "median_score": _median(scores),
        "sd_score": round(statistics.stdev(scores), 4) if len(scores) > 1 else None,
        "wins": wins, "win_rate": round(wins / len(valid), 4) if valid else None,
        "mean_floor": _mean(floors), "cost_usd": round(cost, 6),
        "cost_per_valid_run_usd": round(cost / len(valid), 6) if valid else None,
        "end_reasons": end_reasons,
    }


def build_report(plan: dict, results: list[dict], stop_reason: str | None = None) -> dict:
    labels = [a["label"] for a in plan["arms"]]
    first, second = labels
    by_slot: dict[int, dict] = {}
    for r in results:
        if r.get("valid"):
            by_slot.setdefault(r["slot"], r)
    valid = list(by_slot.values())
    per_arm = {label: _arm_stats(label, [r for r in valid if r["arm"] == label],
                                 [r for r in results if r["arm"] == label]) for label in labels}
    notes, warnings = [], []
    test, win_test = None, None
    if plan["mode"] == "paired":
        pairs = []
        for seed in plan["seeds"]:
            got = {r["arm"]: r for r in valid if r["seed"] == seed}
            if first in got and second in got:
                pairs.append((seed, got[first], got[second]))
        a = [float(p[1]["score"]) for p in pairs]
        b = [float(p[2]["score"]) for p in pairs]
        diffs = [y - x for x, y in zip(a, b)]
        if pairs:
            result = stats.wilcoxon(a, b)
            test = {**result.to_dict(), "pairs": len(pairs), "zero_differences": len(pairs) - result.n,
                    "mean_difference": _mean(diffs), "median_difference": _median(diffs)}
            only_first = sum(p[1]["outcome"] == "won" and p[2]["outcome"] != "won" for p in pairs)
            only_second = sum(p[2]["outcome"] == "won" and p[1]["outcome"] != "won" for p in pairs)
            win_test = {"test": "mcnemar_exact", "only_first_won": only_first, "only_second_won": only_second,
                        "p_value": stats.mcnemar_exact(only_first, only_second)}
        notes.append(f"Paired by seed: Wilcoxon signed-rank on {second} minus {first} score per seed.")
        unverified = [r["slot"] for r in valid if "seed_unverified" in (r.get("warnings") or [])]
        if unverified:
            warnings.append(f"seed not confirmed by the mod for slots {unverified}")
    else:
        a = [float(r["score"]) for r in valid if r["arm"] == first]
        b = [float(r["score"]) for r in valid if r["arm"] == second]
        if a and b:
            test = {**stats.mann_whitney(a, b).to_dict(), "mean_difference": round(statistics.fmean(b) - statistics.fmean(a), 4),
                    "median_difference": round(statistics.median(b) - statistics.median(a), 4)}
            wins_a = per_arm[first]["wins"]
            wins_b = per_arm[second]["wins"]
            win_test = {"test": "fisher_exact", "p_value": stats.fisher_exact(
                [[wins_b, len(b) - wins_b], [wins_a, len(a) - wins_a]])}
        notes.append("Unpaired: seeds could not be set, so the arms were interleaved in time (AB, BA, ...) and "
                     f"compared with a Mann-Whitney U test of {second} against {first}. Run-to-run seed variance "
                     "is not removed, so it needs more runs than a paired test for the same power.")
    if test is not None:
        notes.append("Effect size is the rank-biserial correlation: +1 when every run of the second arm beats "
                     "the first, -1 the other way round, 0 for no difference.")
    invalid = {}
    for r in results:
        if not r.get("valid"):
            invalid[r.get("invalid_reason")] = invalid.get(r.get("invalid_reason"), 0) + 1
    if invalid:
        warnings.append(f"invalid runs (retried up to {plan['max_attempts']} times per slot): {invalid}")
    done = len(by_slot)
    given_up = sum(1 for s in plan["schedule"] if s["slot"] not in by_slot
                   and sum(r["slot"] == s["slot"] for r in results) >= plan["max_attempts"])
    return {
        "name": plan["name"], "mode": plan["mode"], "arms": labels, "comparison": f"{second} vs {first}",
        "metric": "score", "character": plan["character"], "ascension": plan["ascension"],
        "planned_runs": len(plan["schedule"]), "valid_runs": done, "given_up_slots": given_up,
        "complete": done + given_up == len(plan["schedule"]), "stop_reason": stop_reason,
        "cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in results), 6),
        "per_arm": per_arm, "test": test, "win_rate_test": win_test, "notes": notes, "warnings": warnings,
    }


def write_report(directory: str | Path, plan: dict, results: list[dict], stop_reason: str | None = None) -> dict:
    report = build_report(plan, results, stop_reason)
    path = Path(directory) / REPORT_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return report


def list_evals(runs_dir: str | Path) -> list[Path]:
    root = Path(runs_dir) / EVALS_DIR
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and NAME_RE.match(p.name) and (p / PLAN_FILE).exists()),
                  key=lambda p: p.name)


# ---- snapshots

def snapshot(store: str | Path = DEFAULT_STORE, out_dir: str | Path = SNAPSHOT_DIR,
             clock: Callable[[], datetime] = _now) -> Path:
    """Copy the note store to out_dir/notes-<YYYYMMDD>.jsonl (Los Angeles date), read-only.

    An identical snapshot from the same day is reused; a different one gets the time appended.
    """
    store, out_dir = Path(store), Path(out_dir)
    if not store.is_file():
        raise FileNotFoundError(f"no note store at {store}")
    local = clock().astimezone(ZoneInfo(BUDGET_TZ))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"notes-{local:%Y%m%d}.jsonl"
    if target.exists():
        if sha256(target) == sha256(store):
            return target
        target = out_dir / f"notes-{local:%Y%m%d-%H%M%S}.jsonl"
    shutil.copyfile(store, target)
    os.chmod(target, 0o444)
    return target


# ---- CLI

def _model_dests() -> list[str]:
    parser = argparse.ArgumentParser(add_help=False)
    add_model_args(parser)
    return [a.dest for a in parser._actions if a.dest != "goal"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sts_cua.eval", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="copy the note store to a dated, read-only frozen snapshot")
    p.add_argument("--store", default=str(DEFAULT_STORE))
    p.add_argument("--out-dir", default=str(SNAPSHOT_DIR))

    p = sub.add_parser("plan", help="write runs/evals/<name>/plan.json")
    p.add_argument("name")
    p.add_argument("--arms", nargs=2, required=True, metavar="ARM",
                   help="two of: none, frozen:<snapshot path>; the test reports the second minus the first")
    p.add_argument("--seeds", help="seed file, one per line: play each seed once per arm (paired)")
    p.add_argument("--runs-per-arm", type=int, help="runs per arm (default with --seeds: one per seed)")
    p.add_argument("--training-seeds",
                   help="extra seed file of runs memory was written on; refuse to evaluate on them (the ledger's "
                        "memory runs are always checked)")
    p.add_argument("--character", default="the Ironclad", help="character named in the goal and checked in the results")
    p.add_argument("--ascension", type=int, default=0)
    p.add_argument("--game-build", default="unknown")
    p.add_argument("--daily-budget", type=float, default=2.0)
    p.add_argument("--reserve", type=float, default=0.10)
    p.add_argument("--max-cost", type=float, default=0.5,
                   help="USD cap per run; a run starts only with this much left today, so the budget never cuts one short")
    p.add_argument("--max-steps", type=int, default=3000)
    p.add_argument("--max-idle-steps", type=int, default=10)
    p.add_argument("--max-attempts", type=int, default=2, help="tries per slot when a run is invalid")
    add_model_args(p)

    p = sub.add_parser("run", help="play the plan's remaining runs (resumes)")
    p.add_argument("name")
    p.add_argument("--display", default=":99")
    p.add_argument("--state-url", default="http://localhost:15526/api/v1/singleplayer")
    p.add_argument("--compendium-url", default=COMPENDIUM_URL, help="STS2MCP run history: each run's outcome and seed")
    p.add_argument("--max-runs", type=int, help="stop after this many game runs this session")
    p.add_argument("--daily-budget", type=float, help="override the plan's daily budget")
    p.add_argument("--settle", type=float, default=1.0)

    p = sub.add_parser("report", help="print (and rewrite) runs/evals/<name>/report.json")
    p.add_argument("name")
    sub.add_parser("list", help="list experiments")
    for sp in sub.choices.values():
        sp.add_argument("--runs-dir", default="runs")
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        path = snapshot(args.store, args.out_dir)
        print(json.dumps({"snapshot": str(path), "sha256": sha256(path),
                          "arm": f"frozen:{path}"}, indent=2))
        return
    if args.command == "list":
        for d in list_evals(args.runs_dir):
            plan, results = load_plan(d), load_results(d)
            report = build_report(plan, results)
            print(f"{d.name:<24} {plan['mode']:<9} {report['valid_runs']}/{report['planned_runs']} runs  "
                  f"${report['cost_usd']:.2f}  {' vs '.join(reversed(report['arms']))}")
        return
    if args.command == "plan":
        model = {k: getattr(args, k) for k in _model_dests()}
        try:
            seeds = read_seeds(args.seeds) if args.seeds else None
            plan = make_plan(
                args.name, args.arms, model, seeds=seeds, runs_per_arm=args.runs_per_arm,
                training_seeds=(read_seeds(args.training_seeds) if args.training_seeds else [])
                + training_seeds(args.runs_dir),
                character=args.character, ascension=args.ascension, game_build=args.game_build,
                daily_budget_usd=args.daily_budget, reserve_usd=args.reserve, max_cost_per_run=args.max_cost,
                max_steps=args.max_steps, max_idle_steps=args.max_idle_steps, max_attempts=args.max_attempts,
            )
            out = write_plan(args.runs_dir, plan)
        except (ValueError, FileExistsError, OSError) as e:
            raise SystemExit(f"plan: {e}") from None
        print(f"wrote {out / PLAN_FILE}: {plan['mode']}, {len(plan['schedule'])} runs")
        return

    directory = eval_dir(args.runs_dir, args.name)
    if not (directory / PLAN_FILE).exists():
        raise SystemExit(f"no plan at {directory / PLAN_FILE}; make one with `python -m sts_cua.eval plan`")
    if args.command == "report":
        print(json.dumps(write_report(directory, load_plan(directory), load_results(directory)), indent=2))
        return

    from .screen import X11Screen
    from .state import fetch_state, summarize
    from .supervisor import build_model

    plan = load_plan(directory)
    model_args = argparse.Namespace(**{**plan["model"], "max_steps": plan["max_steps"]})
    session = run_eval(
        directory,
        model_factory=lambda goal: build_model(model_args, goal),
        screen_factory=lambda: X11Screen(args.display),
        state_fn=lambda: summarize(fetch_state(args.state_url)),
        history_fn=lambda: fetch_history(args.compendium_url),
        runs_dir=args.runs_dir, max_runs=args.max_runs, daily_budget_usd=args.daily_budget,
        settle_seconds=args.settle,
        meta={"display": args.display, "prompt_file": plan["model"].get("prompt_file"),
              "send_width": plan["model"].get("send_width")},
    )
    print(json.dumps({"stop_reason": session.stop_reason, "runs": len(session.runs)}, indent=2))


if __name__ == "__main__":
    main()
