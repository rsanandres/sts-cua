import json

from sts_cua.actions import Click, Wait
from sts_cua.loop import LoopConfig, run
from sts_cua.models.base import Decision, ScriptedModel, Usage
from sts_cua.runlog import RunLog
from sts_cua.screen import FakeScreen


def read_steps(log):
    return [json.loads(line) for line in (log.dir / "steps.jsonl").read_text().splitlines()]


def test_runs_script_and_logs(tmp_path, frame):
    screen = FakeScreen([frame])
    model = ScriptedModel([[Click(1, 2)], [Click(3, 4), Wait(0)]])
    log = RunLog(tmp_path, meta={"model": model.name})
    summary = run(model, screen, log, LoopConfig(max_steps=2, settle_seconds=0))
    assert screen.performed == [Click(1, 2), Click(3, 4), Wait(0)]
    assert summary["end_reason"] == "max_steps"
    steps = read_steps(log)
    assert [s["step"] for s in steps] == [1, 2]
    assert steps[1]["executed"][0] == {"kind": "click", "x": 3, "y": 4, "button": "left", "count": 1, "modifiers": None}
    assert (log.dir / "screenshots" / "step_0002.png").exists()
    assert json.loads((log.dir / "meta.json").read_text())["model"] == "scripted"


def test_stops_when_idle(tmp_path, frame):
    model = ScriptedModel([])
    log = RunLog(tmp_path, meta={})
    summary = run(model, FakeScreen([frame]), log, LoopConfig(max_steps=10, settle_seconds=0, max_idle_steps=3))
    assert summary["end_reason"] == "idle"
    assert summary["steps"] == 3


def test_history_reaches_model(tmp_path, frame):
    seen = []

    class Recorder:
        name = "recorder"

        def decide(self, obs):
            seen.append([(h.step, h.actions) for h in obs.history])
            return Decision(thought="t", actions=[Click(obs.step, obs.step)])

    run(Recorder(), FakeScreen([frame]), RunLog(tmp_path, meta={}), LoopConfig(max_steps=3, settle_seconds=0))
    assert seen == [[], [(1, [Click(1, 1)])], [(1, [Click(1, 1)]), (2, [Click(2, 2)])]]


def test_stops_at_cost_cap(tmp_path, frame):
    class Pricey:
        name = "pricey"

        def decide(self, obs):
            return Decision(thought="", actions=[Click(0, 0)], usage=Usage(cost_usd=0.4))

    log = RunLog(tmp_path, meta={})
    summary = run(Pricey(), FakeScreen([frame]), log, LoopConfig(max_steps=10, settle_seconds=0, max_cost_usd=1.0))
    assert summary["end_reason"] == "max_cost"
    assert summary["steps"] == 3


def test_model_errors_are_logged_then_stop(tmp_path, frame):
    class Broken:
        name = "broken"

        def decide(self, obs):
            raise RuntimeError("api down")

    log = RunLog(tmp_path, meta={})
    summary = run(Broken(), FakeScreen([frame]), log, LoopConfig(max_steps=10, settle_seconds=0, max_errors=2))
    assert summary["end_reason"] == "model_errors"
    assert "api down" in read_steps(log)[0]["error"]
