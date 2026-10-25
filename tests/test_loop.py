import json

from PIL import Image

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


def test_stops_when_model_says_done(tmp_path, frame):
    class Finisher:
        name = "finisher"

        def decide(self, obs):
            if obs.step == 2:
                return Decision(thought="DONE: on the map", actions=[], done=True)
            return Decision(thought="click", actions=[Click(1, 1)])

    log = RunLog(tmp_path, meta={})
    summary = run(Finisher(), FakeScreen([frame]), log, LoopConfig(max_steps=10, settle_seconds=0))
    assert summary["end_reason"] == "done"
    assert summary["steps"] == 2


def test_waiting_counts_as_idle(tmp_path, frame):
    model = ScriptedModel([[Wait(0)]] * 10)
    log = RunLog(tmp_path, meta={})
    summary = run(model, FakeScreen([frame]), log, LoopConfig(max_steps=10, settle_seconds=0, max_idle_steps=3))
    assert summary["end_reason"] == "idle"
    assert summary["steps"] == 3


def test_state_is_logged_not_shown(tmp_path, frame):
    seen = []

    class Peeker:
        name = "peeker"

        def decide(self, obs):
            seen.append(vars(obs).keys())
            return Decision(thought="", actions=[Click(0, 0)])

    log = RunLog(tmp_path, meta={})
    run(Peeker(), FakeScreen([frame]), log, LoopConfig(max_steps=1, settle_seconds=0),
        state_fn=lambda: {"state_type": "monster", "floor": 1})
    assert "state" not in seen[0]
    assert read_steps(log)[0]["state"] == {"state_type": "monster", "floor": 1}


def test_unchanged_screen_is_flagged_and_stops(tmp_path, frame):
    seen = []

    class Clicker:
        name = "clicker"

        def decide(self, obs):
            seen.append([h.unchanged for h in obs.history])
            return Decision(thought="", actions=[Click(10, 10)])

    log = RunLog(tmp_path, meta={})
    summary = run(Clicker(), FakeScreen([frame]), log, LoopConfig(max_steps=20, settle_seconds=0, max_unchanged=3))
    assert summary["end_reason"] == "stuck"
    assert seen[1] == [True]  # step 2 already knows step 1 changed nothing


def test_changing_screen_is_not_stuck(tmp_path, frame):
    from PIL import Image

    frames = [Image.new("RGB", (1280, 720), (i * 40 % 255, 0, 0)) for i in range(6)]

    class Clicker:
        name = "clicker"

        def decide(self, obs):
            return Decision(thought="", actions=[Click(10, 10)])

    log = RunLog(tmp_path, meta={})
    summary = run(Clicker(), FakeScreen(frames), log, LoopConfig(max_steps=6, settle_seconds=0, max_unchanged=2))
    assert summary["end_reason"] == "max_steps"


def test_stop_fn_ends_the_loop_after_logging(tmp_path, frame):
    states = iter([{"floor": 1}, {"floor": 2}, {"state_type": "game_over"}, {"floor": 9}])
    seen = []

    def stop_fn(state):
        seen.append(state)
        return "run_over:died" if state.get("state_type") == "game_over" else None

    model = ScriptedModel([[Click(1, 1)]] * 10)
    screen = FakeScreen([frame])
    log = RunLog(tmp_path, meta={})
    summary = run(model, screen, log, LoopConfig(max_steps=10, settle_seconds=0),
                  state_fn=lambda: next(states), stop_fn=stop_fn)
    assert summary["end_reason"] == "run_over:died" and summary["steps"] == 3
    assert len(screen.performed) == 2  # the model isn't asked on the stopping step
    steps = read_steps(log)
    assert steps[-1]["state"] == {"state_type": "game_over"} and "thought" not in steps[-1]
    assert len(seen) == 3


def test_notes_reach_model_and_are_logged(tmp_path, frame):
    seen, states = [], iter([{"state_type": "map"}, {"state_type": "monster"}])

    class Reader:
        name = "reader"

        def decide(self, obs):
            seen.append(obs.notes)
            return Decision(thought="", actions=[Click(0, 0)])

    def notes_fn(state):
        return ("- on the map, click a node", ["n0001"]) if state["state_type"] == "map" else ("", [])

    log = RunLog(tmp_path, meta={})
    run(Reader(), FakeScreen([frame]), log, LoopConfig(max_steps=2, settle_seconds=0),
        state_fn=lambda: next(states), notes_fn=notes_fn)
    assert seen == ["- on the map, click a node", ""]
    assert [s["notes_shown"] for s in read_steps(log)] == [["n0001"], []]

    log = RunLog(tmp_path, meta={})
    run(Reader(), FakeScreen([frame]), log, LoopConfig(max_steps=1, settle_seconds=0))
    assert "notes_shown" not in read_steps(log)[0] and seen[-1] == ""


def test_interrupt_still_writes_summary(tmp_path, frame):
    import json

    import pytest

    class Interrupted:
        name = "interrupted"

        def decide(self, obs):
            raise KeyboardInterrupt

    log = RunLog(tmp_path, meta={})
    with pytest.raises(KeyboardInterrupt):
        run(Interrupted(), FakeScreen([frame]), log, LoopConfig(max_steps=5, settle_seconds=0))
    assert json.loads((log.dir / "summary.json").read_text())["end_reason"] == "interrupted"


def test_logs_screen_change_per_step(tmp_path, frame):
    model = ScriptedModel([[Click(1, 2)], [Click(1, 2)]])
    log = RunLog(tmp_path, meta={})
    run(model, FakeScreen([frame]), log, LoopConfig(max_steps=2, settle_seconds=0))
    steps = read_steps(log)
    assert "screen_diff" not in steps[0]  # nothing to compare the first screenshot with
    assert steps[1]["screen_diff"] == 0.0


def test_stops_when_game_state_stalls_even_if_pixels_change(tmp_path):
    # Hover tooltips change the screen each step, but the game state never moves.
    frames = [Image.new("RGB", (64, 36), (i * 40 % 256, 0, 0)) for i in range(6)]
    model = ScriptedModel([[Click(1, 2)]] * 20)
    log = RunLog(tmp_path, meta={})
    state = {"state_type": "monster", "floor": 28, "player": {"hp": 3}}
    summary = run(model, FakeScreen(frames), log, LoopConfig(max_steps=20, settle_seconds=0, max_stalled=4),
                  state_fn=lambda: dict(state))
    assert summary["end_reason"] == "stalled"
    steps = read_steps(log)
    assert steps[-1]["error"] == "stuck: game state unchanged"
    assert len(steps) == 5  # four acting steps after the first, all with the same state


def test_state_change_resets_the_stall_count(tmp_path, frame):
    hps = iter([10, 10, 10, 9, 9, 9, 8, 8, 8, 7])
    model = ScriptedModel([[Click(1, 2)]] * 10)
    log = RunLog(tmp_path, meta={})
    summary = run(model, FakeScreen([Image.new("RGB", (64, 36), (i, 0, 0)) for i in range(0, 200, 20)]), log,
                  LoopConfig(max_steps=10, settle_seconds=0, max_stalled=3, max_unchanged=100),
                  state_fn=lambda: {"player": {"hp": next(hps)}})
    assert summary["end_reason"] == "max_steps"


def test_waits_and_missing_state_do_not_stall(tmp_path):
    frames = [Image.new("RGB", (64, 36), (i, 0, 0)) for i in range(0, 200, 20)]
    log = RunLog(tmp_path, meta={})
    summary = run(ScriptedModel([[Wait(0)]] * 10), FakeScreen(frames), log,
                  LoopConfig(max_steps=6, settle_seconds=0, max_stalled=2, max_idle_steps=100),
                  state_fn=lambda: {"state_type": "map"})
    assert summary["end_reason"] == "max_steps"
    log = RunLog(tmp_path, meta={})
    summary = run(ScriptedModel([[Click(1, 2)]] * 10), FakeScreen(frames), log,
                  LoopConfig(max_steps=6, settle_seconds=0, max_stalled=2), state_fn=lambda: None)
    assert summary["end_reason"] == "max_steps"


def test_menu_screens_never_stall(tmp_path):
    # Timeline reveals and story screens change the picture while the mod's menu state stays put.
    frames = [Image.new("RGB", (64, 36), (i, 0, 0)) for i in range(0, 200, 20)]
    log = RunLog(tmp_path, meta={})
    summary = run(ScriptedModel([[Click(1, 2)]] * 10), FakeScreen(frames), log,
                  LoopConfig(max_steps=8, settle_seconds=0, max_stalled=2),
                  state_fn=lambda: {"state_type": "menu", "menu_screen": "timeline"})
    assert summary["end_reason"] == "max_steps"
