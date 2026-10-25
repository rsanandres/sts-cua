import json
import os
import stat
import time
from datetime import datetime, timezone

import pytest
from fakestates import game_over, menu, room

from sts_cua import eval as evaluation
from sts_cua import stats
from sts_cua.actions import Click
from sts_cua.history import HistoryEntry
from sts_cua.memory.store import NoteStore
from sts_cua.models.base import Decision, Usage
from sts_cua.screen import FakeScreen

MAP_TIP = "On the map, click the glowing node."
MODEL = {"model": "scripted", "thinking": "off"}


class Pricey:
    name = "pricey"

    def __init__(self, goal):
        self.goal = goal
        self.notes = []

    def decide(self, obs):
        self.notes.append(obs.notes)
        return Decision(thought="", actions=[Click(obs.step, obs.step)], usage=Usage(cost_usd=0.1))


class FakeGame:
    """One game run per entry of `runs`: (floor, seed the game actually played, or None for a run that never starts).

    A started run is: the supervisor's main-menu check, then 3 paid steps (menu, map, event on
    that floor) and a game-over screen at 0 HP, which also writes a run-history entry.
    A run that never starts sits on the main menu until the step cap.
    """

    def __init__(self, runs, max_steps=3000):
        self.states = []
        for floor, _ in runs:
            if floor is None:
                self.states += [menu()] * (1 + max_steps)
            else:
                self.states += [menu(), menu(), room("map", 0), room("event", floor), ("over", floor)]
        self.seeds = [seed for floor, seed in runs if floor is not None]
        self.history: list[HistoryEntry] = []

    def __call__(self):
        if not self.states:
            return menu()
        state = self.states.pop(0)
        if isinstance(state, tuple):
            floor = state[1]
            self.history.insert(0, HistoryEntry(
                id=str(len(self.history) + 1), start_time=int(time.time()), win=False, abandoned=False,
                seed=self.seeds.pop(0), characters=["CHARACTER.IRONCLAD"], ascension=0, floor=floor))
            return game_over(floor, 1, 0)
        return state

    def history_fn(self):
        return list(self.history)


@pytest.fixture
def snap(tmp_path):
    store = NoteStore(tmp_path / "memory" / "notes.jsonl")
    store.add("ui", MAP_TIP, {"screen": "map"}).status = "active"
    store.save()
    return evaluation.snapshot(store.path, tmp_path / "memory" / "frozen",
                               clock=lambda: datetime(2026, 10, 25, 19, tzinfo=timezone.utc))


def plan_eval(tmp_path, snap, seeds=("SEED1", "SEED2"), **overrides):
    settings = {"max_cost_per_run": 1.0, "daily_budget_usd": 10.0, **overrides}
    plan = evaluation.make_plan("ab", ["none", f"frozen:{snap}"], MODEL, seeds=list(seeds) if seeds else None,
                                game_build="v0.107.1", **settings)
    return evaluation.write_plan(tmp_path / "runs", plan)


def play(tmp_path, directory, game, models=None, **kwargs):
    def model_factory(goal):
        model = Pricey(goal)
        if models is not None:
            models.append(model)
        return model

    return evaluation.run_eval(directory, model_factory, lambda: FakeScreen([_frame()]), game,
                               history_fn=game.history_fn, runs_dir=tmp_path / "runs", settle_seconds=0,
                               state_retry_seconds=0, sleep=lambda _: None, echo=lambda _: None, **kwargs)


_FRAME = None


def _frame():
    global _FRAME
    if _FRAME is None:
        from PIL import Image

        _FRAME = Image.new("RGB", (1280, 720), (30, 30, 40))
    return _FRAME


# ---- planning

def test_schedule_alternates_arm_order():
    slots = evaluation.schedule(["none", "frozen:x"], ["S1", "S2", "S3"], 3)
    assert [(s["arm"], s["seed"]) for s in slots] == [
        ("none", "S1"), ("frozen:x", "S1"), ("frozen:x", "S2"), ("none", "S2"), ("none", "S3"), ("frozen:x", "S3")]
    assert [s["slot"] for s in slots] == list(range(6)) and [s["pair"] for s in slots] == [0, 0, 1, 1, 2, 2]
    unpaired = evaluation.schedule(["none", "frozen:x"], None, 2)
    assert [s["arm"] for s in unpaired] == ["none", "frozen:x", "frozen:x", "none"]
    assert all(s["seed"] is None for s in unpaired)


def test_read_seeds(tmp_path):
    path = tmp_path / "seeds.txt"
    path.write_text("# evaluation seeds\n2450zar9ef\n\nJDZCSFVCT5  # a comment\n")
    assert evaluation.read_seeds(path) == ["2450ZAR9EF", "JDZCSFVCT5"]
    path.write_text("ABC\nabc\n")
    with pytest.raises(ValueError, match="twice"):
        evaluation.read_seeds(path)
    path.write_text("not a seed!\n")
    with pytest.raises(ValueError, match="letters and digits"):
        evaluation.read_seeds(path)


def test_make_plan(tmp_path, snap):
    plan = evaluation.make_plan("ab", ["none", f"frozen:{snap}"], MODEL, seeds=["A", "B", "C"], runs_per_arm=2)
    assert plan["mode"] == "paired" and plan["seeds"] == ["A", "B"] and len(plan["schedule"]) == 4
    assert plan["arms"][1]["memory"] == str(snap) and plan["arms"][1]["sha256"] == evaluation.sha256(snap)
    assert plan["arms"][0] == {"label": "none", "memory": None, "sha256": None}
    unpaired = evaluation.make_plan("ab", ["none", f"frozen:{snap}"], MODEL, runs_per_arm=3)
    assert unpaired["mode"] == "unpaired" and len(unpaired["schedule"]) == 6

    for kwargs, message in [
        ({"arms": ["none", "none"]}, "two different arms"),
        ({"arms": ["none", "memory/notes.jsonl"]}, "an arm is"),
        ({"arms": ["none", f"frozen:{tmp_path / 'missing.jsonl'}"]}, "no snapshot"),
        ({"seeds": ["A", "B"], "runs_per_arm": 3}, "runs per arm"),
        ({"seeds": ["A", "B"], "training_seeds": ["b"]}, "also used for training: B"),
        ({"runs_per_arm": None}, "runs-per-arm"),
        ({"name": "../up", "runs_per_arm": 1}, "name"),
    ]:
        args = {"name": "ab", "arms": ["none", f"frozen:{snap}"], "model": MODEL, **kwargs}
        with pytest.raises(ValueError, match=message):
            evaluation.make_plan(**args)


def test_training_seeds_from_ledger(tmp_path):
    runs = tmp_path / "runs"
    for name, meta in (("r1", {"kind": "game"}), ("r2", {"kind": "game", "eval": {"name": "ab"}}), ("r3", {})):
        (runs / name).mkdir(parents=True)
        (runs / name / "meta.json").write_text(json.dumps(meta))
    lines = [
        {"run_dir": "runs/r1", "memory": "memory/notes.jsonl", "seed": "aaa"},          # a learning run
        {"run_dir": "runs/r2", "memory": "memory/frozen/x.jsonl", "seed": "BBB"},       # an evaluation run
        {"run_dir": "runs/r3", "memory": None, "seed": "CCC"},                          # no memory
        {"run_dir": "runs/r1", "memory": "memory/notes.jsonl", "seed": None},
    ]
    (runs / "ledger.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines) + "{broken\n")
    assert evaluation.training_seeds(runs) == ["AAA"]


def test_write_plan_refuses_to_overwrite(tmp_path, snap):
    directory = plan_eval(tmp_path, snap)
    assert (directory / "plan.json").exists() and directory == tmp_path / "runs" / "evals" / "ab"
    with pytest.raises(FileExistsError):
        plan_eval(tmp_path, snap)


def test_goals():
    plan = {"character": "the Ironclad", "ascension": 0}
    seeded = evaluation.goal_for(plan, {"seed": "2450ZAR9EF"})
    assert "Custom" in seeded and "2450ZAR9EF" in seeded and "Ascension to 0" in seeded and "the Ironclad" in seeded
    standard = evaluation.goal_for(plan, {"seed": None})
    assert "Standard" in standard and "seed" not in standard


# ---- running

def test_paired_eval_runs_every_slot_with_a_frozen_snapshot(tmp_path, snap):
    directory = plan_eval(tmp_path, snap)
    before = snap.read_bytes()
    game = FakeGame([(5, "SEED1"), (8, "seed1"), (9, "SEED2"), (6, "SEED2")])
    models = []
    session = play(tmp_path, directory, game, models=models)
    assert session.stop_reason == "complete" and len(session.runs) == 4

    results = evaluation.load_results(directory)
    assert [(r["slot"], r["arm"], r["seed"], r["seed_played"]) for r in results] == [
        (0, "none", "SEED1", "SEED1"), (1, f"frozen:{snap}", "SEED1", "seed1"),
        (2, f"frozen:{snap}", "SEED2", "SEED2"), (3, "none", "SEED2", "SEED2")]
    assert all(r["valid"] for r in results) and [r["score"] for r in results] == [5, 8, 9, 6]
    assert all(r["outcome_source"] == "compendium" for r in results)

    # The seeded goal went to every game run; only the frozen arm saw notes.
    assert all("SEED" in m.goal and "Custom" in m.goal for m in models)
    frozen = [m for m, r in zip(models, results) if r["arm"] != "none"]
    plain = [m for m, r in zip(models, results) if r["arm"] == "none"]
    assert all(any(MAP_TIP in n for n in m.notes) for m in frozen)
    assert all(not any(m.notes) for m in plain)

    # Frozen: the snapshot is byte for byte the same, and nothing scored or reflected.
    assert snap.read_bytes() == before
    runs = tmp_path / "runs"
    assert not list(runs.glob("*/memory_score.json")) and not list(runs.glob("*/memory_reflect.json"))
    metas = [json.loads((runs / r["run_dir"].split("/")[-1] / "meta.json").read_text()) for r in results]
    assert [m["eval"]["slot"] for m in metas] == [0, 1, 2, 3] and all(m["eval"]["name"] == "ab" for m in metas)
    assert metas[1]["memory"]["path"] == str(snap) and "memory" not in metas[0]
    ledger = [json.loads(l) for l in (runs / "ledger.jsonl").read_text().splitlines()]
    assert [e["memory"] for e in ledger] == [None, str(snap), str(snap), None]
    assert all(e["notes_added"] is None and e["reflect_cost_usd"] is None for e in ledger)

    report = json.loads((directory / "report.json").read_text())
    assert report["complete"] and report["stop_reason"] == "complete" and report["valid_runs"] == 4
    assert report["test"]["test"] == "wilcoxon_signed_rank" and report["test"]["pairs"] == 2
    assert report["test"]["mean_difference"] == 3.0  # frozen minus none: 8-5, 9-6
    assert report["cost_usd"] == pytest.approx(1.2)


def test_frozen_store_would_change_without_freezing(tmp_path, snap):
    """The control: the same session with learning on edits the store, so the freeze is what keeps it fixed."""
    from sts_cua.supervisor import SupervisorConfig, supervise

    store = tmp_path / "live.jsonl"
    store.write_bytes(snap.read_bytes())
    game = FakeGame([(5, "S")])
    config = SupervisorConfig(runs_dir=tmp_path / "runs", settle_seconds=0, state_retry_seconds=0, max_runs=1,
                              daily_budget_usd=10, memory_path=store, reflect=False)
    supervise(config, Pricey, lambda: FakeScreen([_frame()]), game, echo=lambda _: None)
    assert store.read_bytes() != snap.read_bytes()


def test_wrong_seed_is_retried(tmp_path, snap):
    directory = plan_eval(tmp_path, snap, seeds=("SEED1",))
    game = FakeGame([(5, "OTHER"), (6, "SEED1"), (7, "SEED1")])
    session = play(tmp_path, directory, game)
    assert session.stop_reason == "complete"
    results = evaluation.load_results(directory)
    assert [(r["slot"], r["attempt"], r["valid"], r["invalid_reason"]) for r in results] == [
        (0, 1, False, "seed_mismatch"), (0, 2, True, None), (1, 1, True, None)]
    report = evaluation.build_report(evaluation.load_plan(directory), results)
    assert report["per_arm"]["none"]["runs"] == 1 and report["per_arm"]["none"]["invalid_runs"] == 1
    assert report["per_arm"]["none"]["mean_score"] == 6.0
    assert any("seed_mismatch" in w for w in report["warnings"])


def test_runs_that_never_start(tmp_path, snap):
    directory = plan_eval(tmp_path, snap, seeds=("S1", "S2"), max_steps=2)
    game = FakeGame([(None, None)] * 3, max_steps=2)
    session = play(tmp_path, directory, game)
    assert session.stop_reason == "runs_not_starting"
    results = evaluation.load_results(directory)
    assert [(r["slot"], r["invalid_reason"]) for r in results] == [(0, "not_started"), (0, "not_started"),
                                                                   (1, "not_started")]
    report = evaluation.build_report(evaluation.load_plan(directory), results)
    assert report["given_up_slots"] == 1 and report["test"] is None and not report["complete"]


def test_budget_stops_and_resume_skips_finished_runs(tmp_path, snap):
    # $1.55 a day, $1 cap per run kept in reserve, $0.30 per run: two runs fit today ($0.95 is left after them).
    directory = plan_eval(tmp_path, snap, daily_budget_usd=1.55)
    game = FakeGame([(5, "SEED1"), (8, "SEED1"), (9, "SEED2"), (6, "SEED2")])
    first = play(tmp_path, directory, game)
    assert first.stop_reason == "budget" and [r["slot"] for r in first.runs] == [0, 1]
    assert json.loads((directory / "report.json").read_text())["stop_reason"] == "budget"

    # Same day, still out of budget: nothing runs.
    again = play(tmp_path, directory, game)
    assert again.stop_reason == "budget" and again.runs == []

    resumed = play(tmp_path, directory, game, daily_budget_usd=10.0)
    assert resumed.stop_reason == "complete" and [r["slot"] for r in resumed.runs] == [2, 3]
    results = evaluation.load_results(directory)
    assert [r["slot"] for r in results] == [0, 1, 2, 3] and all(r["valid"] for r in results)
    assert play(tmp_path, directory, game).runs == []  # nothing left


def test_max_runs_per_session(tmp_path, snap):
    directory = plan_eval(tmp_path, snap)
    game = FakeGame([(5, "SEED1"), (8, "SEED1"), (9, "SEED2"), (6, "SEED2")])
    session = play(tmp_path, directory, game, max_runs=1)
    assert session.stop_reason == "max_runs" and len(session.runs) == 1


def test_changed_snapshot_stops_the_eval(tmp_path, snap):
    directory = plan_eval(tmp_path, snap)
    os.chmod(snap, 0o644)
    snap.write_text(snap.read_text() + "\n")
    game = FakeGame([(5, "SEED1"), (8, "SEED1")])
    session = play(tmp_path, directory, game)
    assert session.stop_reason == "snapshot_changed" and [r["arm"] for r in session.runs] == ["none"]


def test_unpaired_eval_uses_standard_runs(tmp_path, snap):
    directory = plan_eval(tmp_path, snap, seeds=None, runs_per_arm=2)
    game = FakeGame([(5, "R1"), (8, "R2"), (9, "R3"), (6, "R4")])
    models = []
    assert play(tmp_path, directory, game, models=models).stop_reason == "complete"
    assert all("Standard" in m.goal for m in models)
    results = evaluation.load_results(directory)
    assert all(r["valid"] and not r["warnings"] for r in results)  # no seed to check
    report = evaluation.build_report(evaluation.load_plan(directory), results)
    assert report["mode"] == "unpaired" and report["test"]["test"] == "mann_whitney_u"
    assert "Unpaired" in report["notes"][0]


# ---- the report

def fixture_results(plan, scores, outcomes=None, invalid=()):
    out = []
    for slot in plan["schedule"]:
        i = slot["slot"]
        if i in invalid:
            out.append({**slot, "attempt": 1, "valid": False, "invalid_reason": "seed_mismatch", "warnings": [],
                        "score": 50, "floor": 50, "outcome": "died", "cost_usd": 0.2})
        if i >= len(scores):
            continue
        out.append({**slot, "attempt": 1, "valid": True, "invalid_reason": None, "warnings": [],
                    "score": scores[i], "floor": min(scores[i], 50), "outcome": (outcomes or {}).get(i, "died"),
                    "cost_usd": 0.5, "end_reason": "run_over:died"})
    return out


def test_report_paired_fixture(snap):
    seeds = [f"S{i}" for i in range(8)]
    plan = evaluation.make_plan("ab", ["none", f"frozen:{snap}"], MODEL, seeds=seeds)
    none_scores = [7, 12, 9, 20, 15, 6, 11, 30]
    frozen_scores = [11, 15, 9, 37.33, 21, 5, 14, 100]
    by_slot = {}
    for slot in plan["schedule"]:
        by_slot[slot["slot"]] = (none_scores if slot["arm"] == "none" else frozen_scores)[slot["pair"]]
    scores = [by_slot[i] for i in range(16)]
    won = {s["slot"]: "won" for s in plan["schedule"] if s["arm"] != "none" and s["pair"] == 7}
    report = evaluation.build_report(plan, fixture_results(plan, scores, won, invalid=(3,)))

    expected = stats.wilcoxon(none_scores, frozen_scores)
    assert report["test"]["p_value"] == pytest.approx(expected.p_value)
    assert report["test"]["pairs"] == 8 and report["test"]["zero_differences"] == 1 and report["test"]["n"] == 7
    assert report["test"]["effect_size"] == pytest.approx(expected.effect_size)
    assert report["test"]["median_difference"] == 3.5
    frozen = report["per_arm"][f"frozen:{snap}"]
    assert frozen["wins"] == 1 and frozen["win_rate"] == 0.125
    assert frozen["mean_score"] == pytest.approx(sum(frozen_scores) / 8, abs=1e-4)
    assert report["per_arm"]["none"]["median_score"] == 11.5
    none = report["per_arm"]["none"]
    assert plan["schedule"][3]["arm"] == "none" and none["runs"] == 8 and none["invalid_runs"] == 1
    assert frozen["invalid_runs"] == 0
    assert report["win_rate_test"] == {"test": "mcnemar_exact", "only_first_won": 0, "only_second_won": 1,
                                       "p_value": 1.0}
    assert report["cost_usd"] == pytest.approx(16 * 0.5 + 0.2)
    assert report["complete"] and report["valid_runs"] == 16


def test_report_unpaired_fixture(snap):
    plan = evaluation.make_plan("ab", ["none", f"frozen:{snap}"], MODEL, runs_per_arm=4)
    scores = [3, 5, 9, 8, 5, 9, 7, 12]  # slots alternate none, frozen, frozen, none, ...
    report = evaluation.build_report(plan, fixture_results(plan, scores))
    a = [s for s, slot in zip(scores, plan["schedule"]) if slot["arm"] == "none"]
    b = [s for s, slot in zip(scores, plan["schedule"]) if slot["arm"] != "none"]
    assert a == [3, 8, 5, 12] and b == [5, 9, 9, 7]
    expected = stats.mann_whitney(a, b)
    assert report["test"]["test"] == "mann_whitney_u" and report["test"]["p_value"] == pytest.approx(expected.p_value)
    assert report["test"]["mean_difference"] == pytest.approx(7.5 - 7.0)
    assert report["win_rate_test"]["test"] == "fisher_exact" and report["win_rate_test"]["p_value"] == 1.0


def test_report_partial_pairs(snap):
    plan = evaluation.make_plan("ab", ["none", f"frozen:{snap}"], MODEL, seeds=["S1", "S2"])
    report = evaluation.build_report(plan, fixture_results(plan, [5, 8, 9]))  # pair 2 half done
    assert report["test"]["pairs"] == 1 and report["valid_runs"] == 3 and not report["complete"]
    assert evaluation.build_report(plan, [])["test"] is None


# ---- snapshots and the CLI

def test_snapshot(tmp_path):
    store = NoteStore(tmp_path / "notes.jsonl")
    store.add("ui", MAP_TIP)
    store.save()
    out = tmp_path / "frozen"
    day = lambda: datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc)  # noqa: E731  Oct 25 in Los Angeles
    first = evaluation.snapshot(store.path, out, clock=day)
    assert first.name == "notes-20261025.jsonl" and first.read_bytes() == store.path.read_bytes()
    assert not first.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert evaluation.snapshot(store.path, out, clock=day) == first  # unchanged store: same snapshot
    store.add("ui", "Another note.")
    store.save()
    second = evaluation.snapshot(store.path, out, clock=day)
    assert second.name == "notes-20261025-233000.jsonl" and second.read_bytes() != first.read_bytes()
    with pytest.raises(FileNotFoundError):
        evaluation.snapshot(tmp_path / "none.jsonl", out)


def test_cli_plan_report_list(tmp_path, snap, capsys):
    seeds = tmp_path / "seeds.txt"
    seeds.write_text("A1\nB2\n")
    runs = str(tmp_path / "runs")
    evaluation.main(["plan", "ab", "--arms", "none", f"frozen:{snap}", "--seeds", str(seeds), "--model", "scripted",
                     "--max-cost", "0.4", "--runs-dir", runs])
    plan = json.loads((tmp_path / "runs" / "evals" / "ab" / "plan.json").read_text())
    assert plan["seeds"] == ["A1", "B2"] and plan["model"]["model"] == "scripted"
    assert plan["budget"]["max_cost_per_run"] == 0.4 and "goal" not in plan["model"]
    with pytest.raises(SystemExit, match="exists"):
        evaluation.main(["plan", "ab", "--arms", "none", f"frozen:{snap}", "--runs-per-arm", "1", "--runs-dir", runs])
    capsys.readouterr()
    evaluation.main(["report", "ab", "--runs-dir", runs])
    assert json.loads(capsys.readouterr().out)["planned_runs"] == 4
    evaluation.main(["list", "--runs-dir", runs])
    assert "ab" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no plan"):
        evaluation.main(["report", "missing", "--runs-dir", runs])
