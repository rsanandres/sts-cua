import json
from datetime import date, datetime, timezone

from fakestates import fight, game_over, menu, room

from sts_cua.actions import Click
from sts_cua.models.base import Decision, Usage
from sts_cua.screen import FakeScreen
from sts_cua.supervisor import RESET_GOAL, SupervisorConfig, spent_on, spent_today, supervise


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

    def decide(self, obs):
        return Decision(thought="", actions=[Click(obs.step, obs.step)], usage=Usage(cost_usd=0.1))


def run_session(tmp_path, frame, game, **overrides):
    goals = []

    def model_factory(goal):
        goals.append(goal)
        return Pricey(goal)

    settings = {"daily_budget_usd": 1.0, "reserve_usd": 0.45, **overrides}
    config = SupervisorConfig(runs_dir=tmp_path, settle_seconds=0, state_retry_seconds=0, game_build="v0.107.1",
                              **settings)
    session = supervise(config, model_factory, lambda: FakeScreen([frame]), game, echo=lambda _: None)
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
