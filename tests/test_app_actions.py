import json
import time

import pytest
from test_app_api import fake_agent

from sts_cua.app.routes import ApiError, supervisor_argv
from sts_cua.models.factory import LOCAL_MODELS
from sts_cua.supervisor import main as supervisor_main

PY = "/venv/bin/python"


def argv(body):
    return supervisor_argv(body, PY, ":99")[0]


def test_supervisor_argv_defaults_and_flags():
    assert argv({"model": "scripted"}) == [PY, "-m", "sts_cua.supervisor", "--display", ":99", "--model", "scripted",
                                           "--daily-budget", "2"]
    assert argv({"model": "claude-sonnet-5-5", "thinking": "on", "effort": "high", "daily_budget": 1.25,
                 "max_runs": 3, "memory": True, "game_build": "v0.107.1"}) == [
        PY, "-m", "sts_cua.supervisor", "--display", ":99", "--model", "claude-sonnet-5-5", "--daily-budget", "1.25",
        "--thinking", "on", "--effort", "high", "--max-runs", "3", "--game-build", "v0.107.1",
        "--memory", "memory/notes.jsonl"]
    assert argv({"model": "qwen3-vl-8b", "thinking": "on", "max_runs": None, "game_build": None, "memory": False}) == [
        PY, "-m", "sts_cua.supervisor", "--display", ":99", "--model", "qwen3-vl-8b", "--daily-budget", "2"]


def test_supervisor_accepts_the_argv():
    """Every flag the app builds parses with the real supervisor CLI (stopping before any work)."""
    import argparse
    from unittest import mock

    full = argv({"model": "claude-haiku-5-5", "thinking": "on", "effort": "medium", "daily_budget": 5,
                 "max_runs": 2, "memory": True, "game_build": "v0.107.1"})
    seen = {}

    def stop(self, args=None, namespace=None):
        seen["args"] = original(self, args, namespace)
        raise SystemExit(0)

    original = argparse.ArgumentParser.parse_args
    with mock.patch.object(argparse.ArgumentParser, "parse_args", stop), pytest.raises(SystemExit):
        supervisor_main(full[3:])
    args = seen["args"]
    assert (args.model, args.thinking, args.effort, args.daily_budget, args.max_runs, args.memory, args.game_build) == (
        "claude-haiku-5-5", "on", "medium", 5.0, 2, "memory/notes.jsonl", "v0.107.1")


def test_allowlist_and_clamping():
    for model in ("claude-haiku-5-5", "claude-sonnet-5-5", "scripted", *LOCAL_MODELS):
        assert argv({"model": model})[6] == model
    for bad in ({}, {"model": "claude-opus-5-5"}, {"model": "scripted; rm -rf ~"}, {"model": ["scripted"]},
                {"model": "scripted", "thinking": "maybe"}, {"model": "scripted", "effort": "max"},
                {"model": "scripted", "daily_budget": "1"}, {"model": "scripted", "daily_budget": True},
                {"model": "scripted", "max_runs": 1.5}, {"model": "scripted", "max_runs": "2"},
                {"model": "scripted", "memory": "yes"}, {"model": "scripted", "game_build": "v1 --model x"},
                {"model": "scripted", "game_build": "../x/.."}, {"model": "scripted", "game_build": 7}):
        with pytest.raises(ApiError) as e:
            argv(bad)
        assert e.value.status == 400, bad
    assert argv({"model": "scripted", "daily_budget": 99})[-1] == "5"
    assert argv({"model": "scripted", "daily_budget": -3})[-1] == "0"
    assert argv({"model": "scripted", "max_runs": 0})[-2:] == ["--max-runs", "1"]
    assert argv({"model": "scripted", "max_runs": 10**9})[-2:] == ["--max-runs", "1000"]
    assert argv({"model": "scripted", "max_runs": 4.0})[-2:] == ["--max-runs", "4"]


def test_runs_start_launches_supervisor_and_409s(app_server):
    c, launcher = app_server.client, app_server.launcher
    status, body = c.post("/api/runs/start", {"model": "claude-haiku-5-5", "daily_budget": 12, "memory": True})
    assert status == 200 and body["started"] is True and body["pid"] == launcher.procs[0].pid
    call = launcher.calls[0]
    assert call["argv"] == [PY, "-m", "sts_cua.supervisor", "--display", ":99", "--model", "claude-haiku-5-5",
                            "--daily-budget", "5", "--thinking", "off", "--effort", "low",
                            "--memory", "memory/notes.jsonl"]
    assert call["cwd"] == app_server.repo and call["env"]["PYTHONUNBUFFERED"] == "1"

    pid_file = json.loads((app_server.state_dir / "run" / "supervisor.pid").read_text())
    assert pid_file["pid"] == body["pid"] and pid_file["argv"] == call["argv"]
    assert (app_server.state_dir / "logs" / "supervisor.log").read_text().startswith("--- ")

    status_body = c.get("/api/status")[1]
    assert status_body["processes"]["supervisor"]["pid"] == body["pid"]
    assert status_body["budget"]["cap_usd"] == 5.0

    status, body = c.post("/api/runs/start", {"model": "scripted"})
    assert status == 409 and "error" in body
    assert len(launcher.calls) == 1

    assert c.post("/api/runs/stop")[0] == 200
    launcher.procs[0].wait(timeout=5)  # sleep dies on SIGINT
    assert not c.get("/api/status")[1]["processes"]["supervisor"]["running"]
    assert not (app_server.state_dir / "run" / "supervisor.pid").exists()
    assert c.post("/api/runs/start", {"model": "scripted"})[0] == 200


def test_runs_start_409_on_external_supervisor(app_server):
    proc = fake_agent("python -m sts_cua.supervisor --display :99 --model scripted")
    try:
        time.sleep(0.2)
        assert app_server.client.post("/api/runs/start", {"model": "scripted"})[0] == 409
        assert app_server.launcher.calls == []
    finally:
        proc.kill()
        proc.wait()


def test_runs_start_bad_body_is_400(app_server):
    assert app_server.client.post("/api/runs/start", {"model": "gpt-6"})[0] == 400
    assert app_server.launcher.calls == []


def test_pid_file_survives_app_restart(app_server):
    from sts_cua.app.procs import ProcessManager

    pid = app_server.client.post("/api/runs/start", {"model": "scripted"})[1]["pid"]
    fresh = ProcessManager(app_server.state_dir)  # a restarted app: no handles, only the pid file
    assert fresh.managed("supervisor")["pid"] == pid
    assert fresh.info("supervisor")["running"]
    app_server.launcher.kill_all()
    assert fresh.managed("supervisor") is None


def test_stale_pid_file_with_reused_pid_is_ignored(tmp_path):
    import os

    from sts_cua.app.procs import ProcessManager

    procs = ProcessManager(tmp_path)
    (tmp_path / "run").mkdir()
    # Our own pid is alive, but its start time doesn't match the record.
    (tmp_path / "run" / "vllm.pid").write_text(json.dumps({"pid": os.getpid(), "start_ticks": 1}))
    assert procs.managed("vllm") is None
    assert not (tmp_path / "run" / "vllm.pid").exists()


def test_stop_escalates_to_sigterm(tmp_path):
    import subprocess

    from sts_cua.app.procs import ProcessManager

    ready = tmp_path / "ready"

    def stubborn(argv, cwd, env, log):
        return subprocess.Popen(["bash", "-c", f"trap '' INT; touch {ready}; sleep 30"], start_new_session=True)

    procs = ProcessManager(tmp_path, launcher=stubborn, find=lambda args: [])
    procs.start("supervisor", ["x"], cwd=tmp_path)
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:  # SIGINT before the trap would just kill bash
        time.sleep(0.02)
    assert procs.stop("supervisor", grace=0.5)
    assert "supervisor" in procs.stopping
    deadline = time.monotonic() + 5
    while procs.managed("supervisor") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert procs.managed("supervisor") is None


def test_stack_up_and_down(app_server):
    c, launcher = app_server.client, app_server.launcher
    assert c.post("/api/stack/up") == (200, {"started": True})
    assert launcher.argv() == ["bash", str(app_server.repo / "tools" / "stack_up.sh")]
    assert c.get("/api/status")[1]["busy"] == "stack_up"
    assert c.post("/api/stack/up")[0] == 409

    with (app_server.state_dir / "logs" / "stack.log").open("a") as f:
        f.write("[12:00:00] stack is up: Steam + Spire 2 on :99.\n")
    assert c.get("/api/status")[1]["busy"] is None

    assert c.post("/api/stack/down") == (200, {"started": True})
    assert launcher.argv() == ["bash", str(app_server.repo / "tools" / "stack_down.sh")]
    launcher.procs[0].wait(timeout=5)  # the waiting stack_up.sh is killed
    assert c.get("/api/status")[1]["busy"] == "stack_down"
    assert c.post("/api/stack/down")[0] == 409
    assert c.post("/api/stack/up")[0] == 409


def test_vllm_start_stop(app_server):
    """The old /api/vllm/* endpoints are aliases of /api/model/* (mode solo)."""
    c, launcher = app_server.client, app_server.launcher
    assert c.post("/api/vllm/start", {"model": "claude-haiku-5-5"})[0] == 400
    assert c.post("/api/vllm/start", {})[0] == 400
    status, body = c.post("/api/vllm/start", {"model": "qwen3-vl-8b"})
    assert status == 200 and body["started"] is True and body["mode"] == "solo"
    assert launcher.argv() == ["/opt/vllm/bin/vllm", "serve", "Qwen/Qwen3-VL-8B-Instruct-FP8",
                               "--gpu-memory-utilization", "0.55", "--max-model-len", "8192", "--max-num-seqs", "1",
                               "--limit-mm-per-prompt", '{"image":1,"video":0}', "--host", "127.0.0.1",
                               "--port", "8000"]
    assert c.get("/api/status")[1]["busy"] == "model_start"
    assert c.post("/api/vllm/start", {"model": "qwen3-vl-8b"})[0] == 409
    app_server.ready.append(True)  # the server answers /v1/models
    assert c.get("/api/status")[1]["busy"] is None
    assert c.post("/api/vllm/stop")[1]["stopped"] is True
    launcher.procs[0].wait(timeout=5)
    assert not c.get("/api/status")[1]["processes"]["model"]["running"]


def test_wsl_shutdown_needs_confirm_and_replies_first(app_server):
    c, launcher = app_server.client, app_server.launcher
    for body in ({}, {"confirm": "true"}, {"confirm": 1}, {"confirm": False}):
        assert c.post("/api/wsl/shutdown", body)[0] == 400
    assert c.post("/api/wsl/shutdown", {"confirm": True}) == (200, {"shutting_down": True})
    assert c.get("/api/status")[1]["busy"] == "wsl_shutdown"
    deadline = time.monotonic() + 5
    while not launcher.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert launcher.argv()[1:] == ["/c", "wsl", "--shutdown"]
    assert launcher.argv()[0].endswith("cmd.exe")


def test_reflect(app_server):
    from appfakes import OLD_RUN

    c, launcher = app_server.client, app_server.launcher
    assert c.post("/api/memory/reflect", {"run_id": "../etc"})[0] == 400
    assert c.post("/api/memory/reflect", {"run_id": "20990101T000000Z"})[0] == 404
    assert c.post("/api/memory/reflect", {})[0] == 400
    assert c.post("/api/memory/reflect", {"run_id": OLD_RUN}) == (200, {"started": True})
    assert launcher.argv() == [PY, "-m", "sts_cua.memory", "reflect", f"runs/{OLD_RUN}"]
    assert launcher.calls[-1]["cwd"] == app_server.repo
    assert c.post("/api/memory/reflect", {"run_id": OLD_RUN})[0] == 409
    assert c.post("/api/memory/notes/n0001/retire")[0] == 409  # the store is being edited
    assert c.get("/api/status")[1]["busy"] == "reflect"
