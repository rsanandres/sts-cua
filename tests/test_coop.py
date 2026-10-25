import json

import pytest
from coopstates import combat, event_state, game_over_overlay, lobby, map_state, rewards_state, treasure_state
from fakestates import menu, room
from PIL import Image

from sts_cua import coop
from sts_cua import state as solo
from sts_cua.__main__ import coop_hooks, loop_config, parse_args, run_meta
from sts_cua.actions import Click, Wait
from sts_cua.loop import LoopConfig, run
from sts_cua.models.base import Decision, ScriptedModel
from sts_cua.runlog import RunLog
from sts_cua.rules import Ruling
from sts_cua.score import RunOverDetector, run_mode
from sts_cua.screen import FakeScreen


def read_steps(log):
    return [json.loads(line) for line in (log.dir / "steps.jsonl").read_text().splitlines()]


def waiting(raw):
    return coop.waiting(coop.summarize(raw))


# ---- the mod's co-op state

def test_endpoints():
    sp = "http://localhost:15526/api/v1/singleplayer"
    assert coop.multiplayer_url(sp) == "http://localhost:15526/api/v1/multiplayer"
    assert coop.singleplayer_url(coop.multiplayer_url(sp)) == sp


def test_fetch_prefers_multiplayer_then_singleplayer(monkeypatch):
    seen = []
    answers = {"multiplayer": None, "singleplayer": {"state_type": "menu"}}

    def fake(url, timeout=2.0):
        seen.append(url.rsplit("/", 1)[1])
        return answers[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(solo, "fetch_state", fake)
    assert coop.fetch_state() == {"state_type": "menu"} and seen == ["multiplayer", "singleplayer"]
    answers["multiplayer"] = {"state_type": "map"}
    seen.clear()
    assert coop.fetch_state() == {"state_type": "map"} and seen == ["multiplayer"]


def test_summarize_keeps_solo_fields_and_adds_coop():
    s = coop.summarize(combat(local_ready=True))
    assert s["state_type"] == "monster" and s["floor"] == 3 and s["enemies"][0]["name"] == "Nibbit"
    assert s["multiplayer"] is True
    assert s["coop"]["turn"] == "player" and s["coop"]["playable"] == [True, True]
    assert [p["is_local"] for p in s["coop"]["players"]] == [True, False]
    assert s["coop"]["players"][0]["ready"] is True
    assert coop.summarize(None) is None
    # A solo state is unchanged: no coop block, no multiplayer mark.
    solo_state = {"state_type": "map", "run": {"act": 1, "floor": 2}, "map": {"next_options": []}}
    assert coop.summarize(solo_state) == solo.summarize(solo_state)
    assert "multiplayer" not in solo.summarize(solo_state)


def test_game_over_overlay_reads_as_game_over():
    s = coop.summarize(game_over_overlay())
    assert s["state_type"] == "game_over"
    detector = RunOverDetector()
    assert detector(coop.summarize(combat())) is None
    assert detector(s).startswith("run_over:")


@pytest.mark.parametrize("raw, reason", [
    (lobby(), None),
    (lobby(local_ready=True), "lobby_ready"),
    (lobby(local_ready=True, all_ready=True, others_ready=(True,)), "lobby_starting"),
    (combat(), None),
    (combat(local_ready=True), "teammate_end_turn"),
    (combat(local_ready=True, others_ready=(True,), all_ready=True), "turn_ending"),
    (combat(turn="enemy", play_phase=False), "enemy_turn"),
    (combat(local_alive=False), "knocked_out"),
    (map_state(others=(None,)), "teammates_voting"),
    (map_state(others=((3, 4), None, (1, 4))), "teammates_voting"),
    (map_state(others=((3, 4),)), None),
    (map_state(local=(3, 4), others=(None,)), "map_vote"),
    (event_state(others=(None,)), "teammates_voting"),
    (event_state(others=(1,)), None),
    (event_state(local=1, others=(None,)), "event_vote"),
    (treasure_state(local=1, others=(None,)), "treasure_bid"),
    (rewards_state(), "teammate_maybe"),
    (room("shop", 4, game_mode="multiplayer"), "teammate_maybe"),
    (menu(), None),
    (None, "no_state"),
])
def test_waiting(raw, reason):
    assert (waiting(raw) if raw is not None else coop.waiting(None)) == reason


def test_in_session_and_mode():
    assert coop.in_session(coop.summarize(lobby())) and coop.in_session(solo.summarize(lobby()))
    assert coop.in_session(coop.summarize(combat()))
    assert not coop.in_session(solo.summarize({"state_type": "menu", "menu_screen": "main", "options": []}))
    assert run_mode({"mode": "coop"}) == "coop" and run_mode({}) == "solo" and run_mode(None) == "solo"


# ---- the loop while waiting for the teammate

class Waiter:
    name = "waiter"

    def decide(self, obs):
        return Decision(thought="", actions=[Wait(0)])


def test_waiting_steps_are_not_idle(tmp_path, frame):
    log = RunLog(tmp_path, meta={})
    summary = run(Waiter(), FakeScreen([frame]), log, LoopConfig(max_steps=12, settle_seconds=0, max_idle_steps=3),
                  state_fn=lambda: {"state_type": "monster"}, waiting_fn=lambda s: "teammate_end_turn")
    assert summary["end_reason"] == "max_steps" and summary["steps"] == 12
    assert {s["waiting"] for s in read_steps(log)} == {"teammate_end_turn"}


def test_idle_still_ends_when_it_is_the_agents_move(tmp_path, frame):
    reasons = iter(["teammate_end_turn"] * 5 + [None] * 10)
    log = RunLog(tmp_path, meta={})
    summary = run(Waiter(), FakeScreen([frame]), log, LoopConfig(max_steps=20, settle_seconds=0, max_idle_steps=3),
                  state_fn=lambda: {}, waiting_fn=lambda s: next(reasons))
    assert summary["end_reason"] == "idle" and summary["steps"] == 8
    assert "waiting" not in read_steps(log)[-1]


def test_unchanged_screen_is_not_stuck_while_waiting(tmp_path, frame):
    seen = []

    class Clicker:
        name = "clicker"

        def decide(self, obs):
            seen.append([h.unchanged for h in obs.history][-1:])
            return Decision(thought="", actions=[Click(10, 10)])

    log = RunLog(tmp_path, meta={})
    summary = run(Clicker(), FakeScreen([frame]), log, LoopConfig(max_steps=10, settle_seconds=0, max_unchanged=3),
                  state_fn=lambda: {}, waiting_fn=lambda s: "map_vote")
    assert summary["end_reason"] == "max_steps"
    assert seen[2] == [True]  # still flagged to the model


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_idle_timeout_without_progress(tmp_path, frame):
    clock = Clock()

    class SlowWaiter:
        name = "slow"

        def decide(self, obs):
            clock.now += 60
            return Decision(thought="", actions=[Wait(0)])

    log = RunLog(tmp_path, meta={})
    summary = run(SlowWaiter(), FakeScreen([frame]), log,
                  LoopConfig(max_steps=100, settle_seconds=0, max_idle_seconds=30 * 60),
                  state_fn=lambda: {}, waiting_fn=lambda s: "teammate_end_turn", clock=clock, sleep=clock.sleep)
    assert summary["end_reason"] == "idle_timeout" and summary["steps"] == 31
    assert read_steps(log)[-1]["error"].startswith("idle timeout")


def test_progress_resets_the_idle_timeout(tmp_path):
    clock = Clock()
    frames = [Image.new("RGB", (64, 36), (i * 40 % 255, 0, 0)) for i in range(40)]

    class SlowClicker:
        name = "slow"

        def decide(self, obs):
            clock.now += 600
            return Decision(thought="", actions=[Click(1, 1)])

    log = RunLog(tmp_path, meta={})
    summary = run(SlowClicker(), FakeScreen(frames), log,
                  LoopConfig(max_steps=10, settle_seconds=0, max_idle_seconds=30 * 60), clock=clock, sleep=clock.sleep)
    assert summary["end_reason"] == "max_steps"


# ---- rules in the loop

class Counting:
    name = "counting"

    def __init__(self):
        self.notes = []

    def decide(self, obs):
        self.notes.append(obs.notes)
        return Decision(thought="model", actions=[Click(5, 5)])


def test_rule_actions_skip_the_model_and_log_source(tmp_path, frame):
    from sts_cua.actions import Key

    model = Counting()
    rulings = iter([Ruling("end_turn", "no playable card", (Key("e"),)), None,
                    Ruling("vote_map", "matches", directive="Rule for this step: vote for the rest site.")])
    screen = FakeScreen([frame])
    log = RunLog(tmp_path, meta={})
    run(model, screen, log, LoopConfig(max_steps=3, settle_seconds=0), state_fn=lambda: {"state_type": "monster"},
        rules_fn=lambda s: next(rulings), notes_fn=lambda s: ("- a lesson", ["n0001"]))
    steps = read_steps(log)
    assert [s["source"] for s in steps] == ["rule", "model", "model"]
    assert steps[0]["rule"] == "end_turn" and steps[0]["executed"] == [{"kind": "key", "keys": "e", "repeat": 1}]
    assert steps[0]["thought"].startswith("[rule end_turn]") and steps[0]["stop_reason"] == "rule"
    assert "rule" not in steps[1] and steps[2]["rule"] == "vote_map"
    assert model.notes == ["- a lesson", "Rule for this step: vote for the rest site.\n\n- a lesson"]
    assert screen.performed[0] == Key("e")


def test_hold_ends_when_the_wait_reason_changes(tmp_path, frame):
    clock = Clock()
    reasons = ["teammate_end_turn"] * 8 + [None] * 50  # the step's own read, 7 polls, then the turn passes
    states = iter(range(100))
    model = Counting()
    calls = {"n": 0}

    def waiting_fn(state):
        calls["n"] += 1
        return reasons[min(calls["n"] - 1, len(reasons) - 1)]

    def rules_fn(state):
        return Ruling("wait_turn", "teammate", (Wait(30.0),), hold=True) if calls["n"] == 1 else None

    log = RunLog(tmp_path, meta={})
    run(model, FakeScreen([frame]), log, LoopConfig(max_steps=2, settle_seconds=0, poll_seconds=0.5),
        state_fn=lambda: next(states), waiting_fn=waiting_fn, rules_fn=rules_fn, clock=clock, sleep=clock.sleep)
    first = read_steps(log)[0]
    assert first["source"] == "rule" and first["proposed"] == [{"kind": "wait", "seconds": 30.0}]
    assert first["executed"][0]["seconds"] == pytest.approx(4.0)  # 8 polls of 0.5 s: stopped once the teammate ended their turn
    assert len(model.notes) == 1  # only the second step asked the model


def test_solo_steps_log_model_source(tmp_path, frame):
    log = RunLog(tmp_path, meta={})
    run(ScriptedModel([[Click(1, 1)]]), FakeScreen([frame]), log, LoopConfig(max_steps=1, settle_seconds=0))
    step = read_steps(log)[0]
    assert step["source"] == "model" and "rule" not in step and "waiting" not in step


# ---- prompts

def test_coop_prompt_only_in_coop(frame, monkeypatch):
    from sts_cua.models.claude import SYSTEM_PROMPT, ClaudeConfig, ClaudeModel
    from sts_cua.models.qwen import QwenConfig, QwenModel
    from sts_cua.models.qwen import SYSTEM_PROMPT as QWEN_PROMPT
    from sts_cua.models.base import Observation

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    obs = Observation(1, frame, [])
    solo_text = ClaudeModel(ClaudeConfig()).build_request(obs)[0]["system"][0]["text"]
    coop_text = ClaudeModel(ClaudeConfig(coop=True)).build_request(obs)[0]["system"][0]["text"]
    assert solo_text == SYSTEM_PROMPT  # the cached prefix is unchanged with co-op off
    assert coop_text == SYSTEM_PROMPT + coop.PROMPT and "teammate" in coop_text
    assert coop.PROMPT not in QwenModel(QwenConfig()).system_text()
    qwen = QwenModel(QwenConfig(coop=True)).system_text()
    assert qwen.startswith(QWEN_PROMPT + coop.PROMPT) and "# Tools" in qwen


def test_factory_passes_coop(monkeypatch):
    from sts_cua.models.factory import build_model

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert build_model(parse_args(["--coop"])).config.coop is True
    assert build_model(parse_args([])).config.coop is False
    assert build_model(parse_args(["--coop", "--model", "qwen3-vl-8b"])).config.coop is True


# ---- the CLI

def test_coop_defaults_are_fast_and_explicit_flags_win():
    args = parse_args(["--coop"])
    assert (args.max_steps, args.settle, args.history_steps, args.model, args.thinking) == (
        coop.MAX_STEPS, coop.SETTLE_SECONDS, coop.HISTORY_STEPS, "claude-haiku-5-5", "off")
    assert args.goal == coop.GOAL
    args = parse_args(["--coop", "--max-steps", "7", "--settle", "2", "--history-steps", "8", "--goal", "Win"])
    assert (args.max_steps, args.settle, args.history_steps, args.goal) == (7, 2.0, 8, "Win")
    solo_args = parse_args([])
    assert (solo_args.max_steps, solo_args.settle, solo_args.history_steps, solo_args.goal) == (20, 1.0, 8, None)


def test_coop_loop_config_and_hooks():
    args = parse_args(["--coop", "--coop-idle-minutes", "45"])
    config = loop_config(args)
    assert config.max_idle_seconds == 45 * 60 and config.max_idle_steps == coop.MAX_IDLE_STEPS
    hooks = coop_hooks(args)
    assert hooks["waiting_fn"] is coop.waiting and "rules_fn" in hooks and isinstance(hooks["stop_fn"], RunOverDetector)
    assert "rules_fn" not in coop_hooks(parse_args(["--coop", "--no-rules"]))
    assert coop_hooks(parse_args([])) == {} and loop_config(parse_args([])).max_idle_seconds is None


def test_meta_mode():
    meta = run_meta(parse_args(["--coop"]), "m", (1280, 720), None)
    assert meta["mode"] == "coop" and meta["coop_idle_minutes"] == 30.0 and meta["rules"] is True
    assert run_mode(meta) == "coop"
    solo_meta = run_meta(parse_args([]), "m", (1280, 720), None)
    assert "mode" not in solo_meta and run_mode(solo_meta) == "solo"


# ---- co-op runs are not solo data

def test_memory_skips_coop_runs(tmp_path):
    from memory_runs import FakeClient, fight, write_run

    from sts_cua.memory import NoteStore
    from sts_cua.memory.reflect import reflect
    from sts_cua.memory.runs import load_run
    from sts_cua.memory.score import score_run

    store = NoteStore(tmp_path / "notes.jsonl")
    run_dir = write_run(tmp_path, "coop1", fight("Nibbit", 1, 80, 5, shown=["n0001"]),
                        meta={"mode": "coop", "memory": {"seed": 1}})
    finished = load_run(run_dir)
    assert score_run(store, finished) == {"skipped": "co-op run"}
    client = FakeClient([])
    assert reflect(store, finished, client=client)["skipped"] == "co-op run" and client.requests == []


def test_supervisor_leaves_a_coop_lobby_alone(tmp_path, frame):
    from test_supervisor import run_session

    states = [solo.summarize(lobby())] * 3
    session, goals = run_session(tmp_path, frame, lambda: states[0])
    assert session.stop_reason == "coop_session" and goals == [] and session.runs == []
