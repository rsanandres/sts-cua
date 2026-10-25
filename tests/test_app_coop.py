import json
import time

import pytest
from appfakes import write_run
from test_app_api import fake_agent

from sts_cua.__main__ import parse_args
from sts_cua.app.routes import ApiError, coop_argv

PY = "/venv/bin/python"


def argv(body):
    return coop_argv(body, PY, ":99")[0]


def test_coop_argv_defaults_are_the_fast_settings():
    assert argv({}) == [PY, "-m", "sts_cua", "--coop", "--display", ":99", "--model", "claude-haiku-5-5",
                        "--max-cost", "1", "--coop-idle-minutes", "30", "--thinking", "off", "--effort", "low"]
    assert argv({"model": "claude-sonnet-5-5", "thinking": "on", "effort": "medium", "max_cost": 2.5,
                 "idle_minutes": 45, "rules": False, "game_build": "v0.107.1"}) == [
        PY, "-m", "sts_cua", "--coop", "--display", ":99", "--model", "claude-sonnet-5-5", "--max-cost", "2.5",
        "--coop-idle-minutes", "45", "--thinking", "on", "--effort", "medium", "--game-build", "v0.107.1",
        "--no-rules"]
    assert argv({"model": "qwen3-vl-8b", "thinking": "on"})[-2:] == ["--coop-idle-minutes", "30"]


def test_coop_argv_parses_with_the_real_cli():
    args = parse_args(argv({"model": "claude-haiku-5-5", "max_cost": 3, "idle_minutes": 10, "rules": False,
                            "game_build": "v0.107.1"})[3:])
    assert (args.coop, args.model, args.max_cost, args.coop_idle_minutes, args.no_rules, args.game_build) == (
        True, "claude-haiku-5-5", 3.0, 10.0, True, "v0.107.1")


def test_coop_argv_validation_and_clamping():
    for bad in ({"model": "claude-opus-5-5"}, {"model": "x; rm -rf ~"}, {"thinking": "maybe"}, {"effort": "max"},
                {"max_cost": "1"}, {"max_cost": True}, {"idle_minutes": "30"}, {"rules": "yes"},
                {"game_build": "v1 --model x"}, {"game_build": 7}):
        with pytest.raises(ApiError) as e:
            argv(bad)
        assert e.value.status == 400, bad
    assert argv({"max_cost": 99})[9] == "5"
    assert argv({"max_cost": -1})[9] == "0"
    assert argv({"idle_minutes": 1})[11] == "5"
    assert argv({"idle_minutes": 10**6})[11] == "120"


def test_coop_start_stop_and_409s(app_server):
    c, launcher = app_server.client, app_server.launcher
    status, body = c.post("/api/coop/start", {"max_cost": 0.5})
    assert status == 200 and body["started"] is True and body["pid"] == launcher.procs[0].pid
    assert body["settings"]["model"] == "claude-haiku-5-5" and body["settings"]["max_cost"] == 0.5
    call = launcher.calls[0]
    assert call["argv"][:4] == [PY, "-m", "sts_cua", "--coop"] and call["cwd"] == app_server.repo
    assert (app_server.state_dir / "logs" / "coop.log").read_text().startswith("--- ")
    assert c.get("/api/status")[1]["coop"]["pid"] == body["pid"]
    assert c.get("/api/logs/coop")[0] == 200

    assert c.post("/api/coop/start", {})[0] == 409            # one co-op session at a time
    assert c.post("/api/runs/start", {"model": "scripted"})[0] == 409  # nor a supervisor beside it
    assert len(launcher.calls) == 1

    assert c.post("/api/coop/stop")[1]["stopped"] is True
    launcher.procs[0].wait(timeout=5)
    assert not c.get("/api/status")[1]["coop"]["running"]
    assert c.post("/api/coop/stop") == (200, {"stopped": True, "pids": []})


def test_coop_start_409_beside_a_supervisor(app_server):
    proc = fake_agent("python -m sts_cua.supervisor --display :99 --model scripted")
    try:
        time.sleep(0.2)
        assert app_server.client.post("/api/coop/start", {})[0] == 409
        assert app_server.launcher.calls == []
    finally:
        proc.kill()
        proc.wait()


def test_coop_bad_body_is_400(app_server):
    assert app_server.client.post("/api/coop/start", {"model": "gpt-6"})[0] == 400
    assert app_server.launcher.calls == []


def test_coop_runs_are_marked(app_server):
    runs = app_server.repo / "runs"
    run = write_run(runs, "20261025T130000Z", 2, finished=True)
    meta = json.loads((run / "meta.json").read_text())
    (run / "meta.json").write_text(json.dumps({**meta, "mode": "coop"}))
    c = app_server.client
    rows = {r["run_id"]: r for r in c.get("/api/runs")[1]["runs"]}
    assert rows["20261025T130000Z"]["mode"] == "coop"
    assert all("mode" not in r for k, r in rows.items() if k != "20261025T130000Z")
    assert c.get("/api/live")[1]["mode"] == "coop"
