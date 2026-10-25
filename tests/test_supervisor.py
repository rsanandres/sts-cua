import json
from datetime import date, datetime, timezone

import pytest
from fakestates import fight, game_over, menu, room
from memory_runs import FakeClient

from sts_cua import supervisor
from sts_cua.actions import Click
from sts_cua.memory.store import NoteStore
from sts_cua.models.base import Decision, Usage
from sts_cua.screen import FakeScreen
from sts_cua.supervisor import RESET_GOAL, Session, SupervisorConfig, spent_on, spent_today, supervise


def write_summary(runs_dir, name, ended_at, cost):
    (runs_dir / name).mkdir(parents=True)
    (runs_dir / name / "summary.json").write_text(json.dumps({"ended_at": ended_at, "cost_usd": cost}))


def test_budget_days_follow_los_angeles(tmp_path):
    write_summary(tmp_path, "a", "2026-10-25T06:59:00+00:00", 0.5)   # Oct 24, 23:59 PDT
    write_summary(tmp_path, "b", "2026-10-25T07:00:00+00:00", 0.25)  # Oct 25, 00:00 PDT
    write_summary(tmp_path, "c", "2026-10-26T06:30:00+00:00", 0.125)  # Oct 25, 23:30 PDT
    write_summary(tmp_path, "d", "2026-10-25T12:00:00+05:00", 1.0)   # 07:00Z, Oct 25 00:00 PDT
    write_summary(tmp_path, "e", "2026-10-25T10:00:00", 2.0)         # naive: read as UTC
    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "summary.json").write_text("not json")
    assert spent_on(tmp_path, date(2026, 10, 24)) == 0.5
    assert spent_on(tmp_path, date(2026, 10, 25)) == 3.375
    assert spent_on(tmp_path, date(2026, 10, 25), tz="UTC") == 3.75
    # 2026-10-26T03:00Z is still Oct 25 in Los Angeles.
    assert spent_today(tmp_path, datetime(2026, 10, 26, 3, tzinfo=timezone.utc)) == 3.375
    # After DST ends (Nov 1), Los Angeles is UTC-8: 07:30Z is still the day before.
    write_summary(tmp_path, "g", "2026-11-02T07:30:00+00:00", 0.75)
    assert spent_on(tmp_path, date(2026, 11, 1)) == 0.75


def test_budget_counts_reflect_calls_once(tmp_path):
    write_summary(tmp_path, "a", "2026-10-25T18:00:00+00:00", 0.5)
    (tmp_path / "a" / "memory_reflect.json").write_text(json.dumps({"time": "2026-10-25T18:01:00+00:00", "cost_usd": 0.02}))
    write_summary(tmp_path, "b", "2026-10-26T06:59:00+00:00", 0.25)  # Oct 25, 23:59 PDT
    (tmp_path / "b" / "memory_reflect.json").write_text(json.dumps({"time": "2026-10-26T07:01:00+00:00", "cost_usd": 0.04}))
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "memory_reflect.json").write_text(json.dumps({"skipped": "no time"}))
    assert spent_on(tmp_path, date(2026, 10, 25)) == pytest.approx(0.77)
    assert spent_on(tmp_path, date(2026, 10, 26)) == pytest.approx(0.04)  # the reflect call after midnight


class FakeGame:
    """Hands out scripted states, then sits on the main menu."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.states.pop(0) if self.states else menu()


class Pricey:
    name = "pricey"

    def __init__(self, goal):
        self.goal = goal
        self.notes = []  # the notes text on each step

    def decide(self, obs):
        self.notes.append(obs.notes)
        return Decision(thought="", actions=[Click(obs.step, obs.step)], usage=Usage(cost_usd=0.1))


def run_session(tmp_path, frame, game, models=None, client_factory=None, **overrides):
    goals = []

    def model_factory(goal):
        goals.append(goal)
        model = Pricey(goal)
        if models is not None:
            models.append(model)
        return model

    settings = {"daily_budget_usd": 1.0, "reserve_usd": 0.45, **overrides}
    config = SupervisorConfig(runs_dir=tmp_path, settle_seconds=0, state_retry_seconds=0, game_build="v0.107.1",
                              **settings)
    session = supervise(config, model_factory, lambda: FakeScreen([frame]), game, echo=lambda _: None,
                        reflect_client_factory=client_factory)
    return session, goals


def test_two_runs_then_budget(tmp_path, frame):
    game = FakeGame([
        menu(),                                                           # check before run 1
        menu(), room("map", 0), fight("monster", 1, 1, 80, "Nibbit"),   # run 1: 3 paid steps
        game_over(1, 1, 0),                                               # run 1 stops here, unpaid
        game_over(1, 1, 0),                                               # check before run 2: not at menu
        game_over(1, 1, 0), menu(),                                       # reset: 1 paid step, then stops
        menu(),                                                           # check after reset, then run 2 starts
        menu(), room("map", 0), room("event", 1),                         # run 2: 3 paid steps
        menu(options=["continue", "abandon_run"]),                        # left the run: abandoned
    ])
    session, goals = run_session(tmp_path, frame, game)
    assert session.stop_reason == "budget"
    assert [r["outcome"] for r in session.runs] == ["died", "abandoned"]
    assert [r["end_reason"] for r in session.runs] == ["run_over:died", "run_over:abandoned"]
    assert [r["cost_usd"] for r in session.runs] == [0.3, 0.3]
    assert goals[1] == RESET_GOAL and goals[0] == goals[2] != RESET_GOAL
    assert not game.states  # every scripted state was used

    ledger = [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert ledger == session.runs
    first = ledger[0]
    assert first["floor"] == 1 and first["score"] == 1 and first["game_build"] == "v0.107.1" and first["model"] == "pricey"
    score = json.loads((tmp_path / first["run_dir"].split("/")[-1] / "score.json").read_text())
    assert score["combats"][0]["enemies"] == ["Nibbit"] and score["combats"][0]["result"] == "lost"
    kinds = sorted(json.loads(p.read_text())["kind"] for p in tmp_path.glob("*/meta.json"))
    assert kinds == ["game", "game", "reset"]
    assert abs(spent_today(tmp_path, datetime.now(timezone.utc)) - 0.7) < 1e-9


def test_spent_budget_stops_before_any_run(tmp_path, frame):
    write_summary(tmp_path, "earlier", datetime.now(timezone.utc).isoformat(), 0.9)
    session, goals = run_session(tmp_path, frame, FakeGame([]))
    assert session.stop_reason == "budget" and session.runs == [] and goals == []


def test_stops_without_mod_state(tmp_path, frame):
    sleeps = []
    game = lambda: None  # noqa: E731
    config = SupervisorConfig(runs_dir=tmp_path, settle_seconds=0, state_retries=3, state_retry_seconds=2)
    session = supervise(config, Pricey, lambda: FakeScreen([frame]), game, sleep=sleeps.append, echo=lambda _: None)
    assert session.stop_reason == "no_state" and sleeps == [2, 2]


def test_stops_when_runs_never_start(tmp_path, frame):
    session, _ = run_session(tmp_path, frame, FakeGame([]), daily_budget_usd=100, max_steps=2, max_unstarted_runs=2)
    assert session.stop_reason == "runs_not_starting"
    assert [r["outcome"] for r in session.runs] == ["unknown", "unknown"]


def test_max_runs(tmp_path, frame):
    session, _ = run_session(tmp_path, frame, FakeGame([]), daily_budget_usd=100, max_steps=1, max_runs=1)
    assert session.stop_reason == "max_runs" and len(session.runs) == 1


# Memory: a session of two game runs with a reset in between.
def two_game_runs():
    return FakeGame([
        menu(),                                                           # check before run 1
        menu(), room("map", 0), fight("monster", 1, 1, 80, "Nibbit"),   # run 1: 3 paid steps
        game_over(1, 1, 0),                                               # run 1 stops here
        game_over(1, 1, 0),                                               # check before run 2: not at menu
        game_over(1, 1, 0), menu(),                                       # reset: 1 paid step
        menu(),                                                           # check after reset
        menu(), room("map", 0), room("event", 1),                         # run 2: 3 paid steps
        menu(options=["continue", "abandon_run"]),                        # run 2 stops here
    ])


MAP_TIP = "On the map, click the glowing node."
OPS = [
    {"op": "ADD", "kind": "enemy", "keys": {"enemy": "Nibbit"}, "text": "Nibbit hits for 12 on turn 1; Defend first.",
     "reason": "lost the fight"},
    {"op": "UPDATE", "id": "n0001", "text": MAP_TIP + " It pulses.", "reason": "clearer"},
]


def memory_session(tmp_path, frame, client_factory=None, **overrides):
    store = NoteStore(tmp_path / "memory" / "notes.jsonl")
    store.add("ui", MAP_TIP, {"screen": "map"}).status = "active"
    store.save()
    models = []
    settings = {"daily_budget_usd": 10.0, "reserve_usd": 0.1, "max_runs": 2, **overrides}
    session, _ = run_session(tmp_path / "runs", frame, two_game_runs(), models=models, client_factory=client_factory,
                             memory_path=store.path, **settings)
    resets = [p.parent for p in (tmp_path / "runs").glob("*/meta.json") if json.loads(p.read_text())["kind"] == "reset"]
    return session, models, store.path, resets[0] if resets else None


def test_memory_session_shows_notes_scores_and_reflects(tmp_path, frame):
    client = FakeClient(OPS)
    factory_calls = []
    session, models, store_path, reset_dir = memory_session(
        tmp_path, frame, client_factory=lambda: factory_calls.append(1) or client)
    assert session.stop_reason == "max_runs" and len(session.runs) == 2

    # Game runs saw the note on the map; the reset run got no notes at all.
    game_models = [m for m in models if m.goal != RESET_GOAL]
    reset_model = next(m for m in models if m.goal == RESET_GOAL)
    assert all(any(MAP_TIP in n for n in m.notes) for m in game_models)
    assert reset_model.notes == [""]
    reset_steps = [json.loads(l) for l in (reset_dir / "steps.jsonl").read_text().splitlines()]
    assert all("notes_shown" not in s for s in reset_steps)
    assert "memory" not in json.loads((reset_dir / "meta.json").read_text())

    for entry in session.runs:
        run_dir = tmp_path / "runs" / entry["run_dir"].split("/")[-1]
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["memory"]["path"] == str(store_path) and isinstance(meta["memory"]["seed"], int)
        assert meta["memory"]["game_version"] == "v0.107.1"
        steps = [json.loads(l) for l in (run_dir / "steps.jsonl").read_text().splitlines()]
        assert any(s.get("notes_shown") == ["n0001"] for s in steps)
        assert (run_dir / "memory_score.json").exists() and (run_dir / "memory_reflect.json").exists()
    assert not (reset_dir / "memory_score.json").exists() and not (reset_dir / "memory_reflect.json").exists()

    # One reflect call per game run, none for the reset.
    assert len(client.requests) == 2 and len(factory_calls) == 2
    ledger = [json.loads(l) for l in (tmp_path / "runs" / "ledger.jsonl").read_text().splitlines()]
    assert ledger == session.runs
    assert [e["memory"] for e in ledger] == [str(store_path)] * 2
    assert [e["notes_added"] for e in ledger] == [1, 0]  # the second ADD duplicates the first run's note
    assert [e["notes_changed"] for e in ledger] == [1, 1]
    reflect_cost = (3000 * 2.00 + 400 * 10.00) / 1e6
    assert [e["reflect_cost_usd"] for e in ledger] == [pytest.approx(reflect_cost)] * 2
    saved = NoteStore.load(store_path)
    assert [n.id for n in saved.notes] == ["n0001", "n0002"] and saved.get("n0001").text.endswith("It pulses.")

    # Today's spend: 7 paid steps plus both reflect calls.
    assert spent_today(tmp_path / "runs", datetime.now(timezone.utc)) == pytest.approx(0.7 + 2 * reflect_cost)


def test_memory_without_reflect(tmp_path, frame):
    factory_calls = []
    session, models, _, _ = memory_session(tmp_path, frame, client_factory=lambda: factory_calls.append(1), reflect=False)
    assert len(session.runs) == 2 and factory_calls == []
    assert all(any(MAP_TIP in n for n in m.notes) for m in models if m.goal != RESET_GOAL)
    assert all(e["notes_added"] is None and e["reflect_cost_usd"] is None for e in session.runs)
    assert len(list((tmp_path / "runs").glob("*/memory_score.json"))) == 2
    assert not list((tmp_path / "runs").glob("*/memory_reflect.json"))


def test_failing_reflect_does_not_stop_the_session(tmp_path, frame):
    class Broken:
        messages = None  # .create raises AttributeError

    session, _, _, _ = memory_session(tmp_path, frame, client_factory=Broken)
    assert session.stop_reason == "max_runs" and len(session.runs) == 2
    assert all(e["notes_added"] is None for e in session.runs)
    errors = [json.loads(p.read_text()) for p in (tmp_path / "runs").glob("*/memory_error.json")]
    assert len(errors) == 2 and all(e["stage"] == "reflect" and "AttributeError" in e["error"] for e in errors)
    assert len(list((tmp_path / "runs").glob("*/memory_score.json"))) == 2  # scoring still happened


def test_reflect_needs_its_reserve(tmp_path, frame):
    client = FakeClient(OPS)
    # $1 a day with $0.95 held back for reflect: the run is capped at $0.05 (one step), then $0.90 isn't enough.
    session, _, _, _ = memory_session(tmp_path, frame, client_factory=lambda: client, daily_budget_usd=1.0,
                                      reflect_reserve_usd=0.95, max_runs=1)
    assert len(session.runs) == 1 and session.runs[0]["end_reason"] == "max_cost"
    assert client.requests == [] and session.runs[0]["notes_added"] is None
    assert len(list((tmp_path / "runs").glob("*/memory_score.json"))) == 1


def test_no_memory_ledger_fields(tmp_path, frame):
    session, _ = run_session(tmp_path, frame, FakeGame([]), daily_budget_usd=100, max_steps=1, max_runs=1)
    entry = session.runs[0]
    assert entry["memory"] is None and entry["notes_added"] is None and entry["notes_changed"] is None
    assert not list(tmp_path.glob("*/memory_*.json"))
    assert "memory" not in json.loads(next(tmp_path.glob("*/meta.json")).read_text())


def test_cli_memory_flags(monkeypatch):
    configs = []
    monkeypatch.setattr(supervisor, "supervise", lambda config, **_: configs.append(config) or Session([], "test"))
    supervisor.main(["--model", "scripted", "--memory", "memory/notes.jsonl", "--no-reflect", "--reflect-reserve", "0.2"])
    supervisor.main(["--model", "scripted"])
    assert str(configs[0].memory_path) == "memory/notes.jsonl" and configs[0].reflect is False
    assert configs[0].reflect_reserve_usd == 0.2
    assert configs[1].memory_path is None and configs[1].reflect is True and configs[1].reflect_reserve_usd == 0.05
