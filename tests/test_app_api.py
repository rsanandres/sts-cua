import json
import os
import stat
import subprocess
import time

import pytest
from appfakes import LIVE_RUN, OLD_RUN, png_bytes, start_server, step_record, stop_server

from sts_cua.app import files, routes


def fake_agent(cmdline: str) -> subprocess.Popen:
    """A process whose command line reads like cmdline (for pgrep -f)."""
    return subprocess.Popen(["bash", "-c", f'exec -a "{cmdline}" sleep 30'])


def test_status(app_server, tmp_path):
    # A reset run that ended today in Los Angeles, plus a reflect call, count toward the budget.
    run = app_server.repo / "runs" / OLD_RUN
    (run / "memory_reflect.json").write_text(json.dumps({"cost_usd": 0.05, "time": "2026-10-25T18:30:00+00:00"}))
    status, body = app_server.client.get("/api/status")
    assert status == 200
    assert set(body["processes"]) == {"steam", "xvfb", "game", "vllm", "supervisor"}
    for p in body["processes"].values():
        assert p == {"running": False, "pid": None, "since": None}
    assert body["gpu"] is None
    assert body["ram"]["total_mib"] > 0 and 0 <= body["ram"]["used_mib"] <= body["ram"]["total_mib"]
    assert body["budget"] == {"date": "2026-10-25", "spent_usd": 0.3, "cap_usd": 2.0}
    assert body["mod"] == {"reachable": False, "state_type": None, "act": None, "floor": None, "hp": None,
                           "max_hp": None}
    assert body["busy"] is None


def test_gpu_reading(tmp_path):
    smi = tmp_path / "nvidia-smi"
    smi.write_text("#!/bin/sh\necho '4321, 24564'\n")
    smi.chmod(smi.stat().st_mode | stat.S_IEXEC)
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 9\n")
    broken.chmod(0o755)
    assert routes.gpu_memory((str(tmp_path / "missing"), str(broken), str(smi))) == {"used_mib": 4321,
                                                                                    "total_mib": 24564}
    assert routes.gpu_memory((str(broken),)) is None


def test_status_sees_external_supervisor(app_server):
    proc = fake_agent("python -m sts_cua.supervisor --display :99")
    try:
        time.sleep(0.2)
        sup = app_server.client.get("/api/status")[1]["processes"]["supervisor"]
        assert sup["running"] and sup["pid"] == proc.pid and sup["since"]
    finally:
        proc.kill()
        proc.wait()


def test_logs(app_server):
    log_dir = app_server.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "supervisor.log").write_text("".join(f"line {i}\n" for i in range(500)))
    c = app_server.client
    assert c.get("/api/logs/supervisor?lines=3") == (200, {"name": "supervisor", "lines": ["line 497", "line 498",
                                                                                             "line 499"]})
    assert len(c.get("/api/logs/supervisor")[1]["lines"]) == 200
    assert len(c.get("/api/logs/supervisor?lines=99999")[1]["lines"]) == 500
    assert c.get("/api/logs/vllm") == (200, {"name": "vllm", "lines": []})
    assert c.get("/api/logs/passwd")[0] == 404
    assert c.get("/api/logs/supervisor?lines=abc")[0] == 400


def test_ledger_newest_first(app_server):
    c = app_server.client
    status, body = c.get("/api/ledger")
    assert status == 200 and [e["score"] for e in body["entries"]] == [2.0, 1.0, 0.0]
    assert [e["score"] for e in c.get("/api/ledger?limit=2")[1]["entries"]] == [2.0, 1.0]


def test_runs_list_and_detail(app_server):
    c = app_server.client
    status, body = c.get("/api/runs")
    assert status == 200
    assert [r["run_id"] for r in body["runs"]] == [LIVE_RUN, OLD_RUN]
    live, old = body["runs"]
    assert old == {"run_id": OLD_RUN, "kind": "game", "model": "claude-haiku-5-5",
                   "started_at": "2026-10-25T00:00:00+00:00", "end_reason": "run_over:died", "steps": 3,
                   "cost_usd": 0.25, "outcome": "died", "floor": 7, "score": 7.0}
    assert live["end_reason"] is None and live["score"] is None and live["steps"] is None
    assert [r["run_id"] for r in c.get("/api/runs?limit=1")[1]["runs"]] == [LIVE_RUN]

    status, body = c.get(f"/api/runs/{OLD_RUN}")
    assert status == 200 and body["meta"]["kind"] == "game" and body["summary"]["steps"] == 3
    assert body["score"]["outcome"] == "died"
    assert c.get(f"/api/runs/{LIVE_RUN}")[1]["summary"] is None
    assert c.get("/api/runs/20990101T000000Z")[0] == 404


def test_run_steps_paged_without_raw(app_server):
    c = app_server.client
    status, body = c.get(f"/api/runs/{OLD_RUN}/steps")
    assert status == 200 and body["total"] == 3
    assert [s["step"] for s in body["steps"]] == [1, 2, 3]
    assert all("raw" not in s for s in body["steps"])
    body = c.get(f"/api/runs/{OLD_RUN}/steps?offset=1&limit=1")[1]
    assert body["total"] == 3 and [s["step"] for s in body["steps"]] == [2]
    assert c.get(f"/api/runs/{OLD_RUN}/steps?offset=-5")[1]["steps"][0]["step"] == 1


def test_screenshot(app_server):
    status, body, headers = app_server.client.request(f"/api/runs/{OLD_RUN}/screenshots/step_0002.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body == png_bytes()


@pytest.mark.parametrize("path", [
    f"/api/runs/{OLD_RUN}/screenshots/..%2f..%2f..%2foutside.png",
    f"/api/runs/{OLD_RUN}/screenshots/../../../outside.png",
    "/api/runs/../screenshots/outside.png",
    "/api/runs/..",
    "/api/runs/../steps",
    f"/api/runs/{OLD_RUN}/screenshots/..",
    f"/api/runs/{OLD_RUN}/screenshots/meta.json",
    f"/api/runs/{OLD_RUN}/screenshots/step_9999.png",
    "/api/runs/not-a-run",
    "/api/runs/%2e%2e/steps",
    "/api/logs/..%2f..%2fapp-token",
])
def test_path_traversal_rejected(app_server, path):
    status, body = app_server.client.get(path)
    assert status in (400, 404)
    assert isinstance(body, dict) and "error" in body


def test_symlink_out_of_runs_rejected(app_server, tmp_path):
    shots = app_server.repo / "runs" / OLD_RUN / "screenshots"
    os.symlink(tmp_path / "outside.png", shots / "link.png")
    os.symlink(tmp_path, app_server.repo / "runs" / "escape")
    assert app_server.client.get(f"/api/runs/{OLD_RUN}/screenshots/link.png")[0] == 404
    assert app_server.client.get("/api/runs/escape")[0] == 404


def test_inside():
    from pathlib import Path

    base = Path("/tmp")
    assert files.inside(base, "a", "b.png") == Path("/tmp/a/b.png").resolve()
    for bad in ("..", ".", "", "a/b", "a\\b", "a b", "a\x00"):
        assert files.inside(base, bad) is None


def test_memory_notes(app_server):
    c = app_server.client
    status, body = c.get("/api/memory/notes")
    assert status == 200 and body["path"].endswith("memory/notes.jsonl")
    assert [n["id"] for n in body["notes"]] == ["n0001", "n0002"]
    assert [n["id"] for n in c.get("/api/memory/notes?status=active")[1]["notes"]] == ["n0002"]
    assert [n["id"] for n in c.get("/api/memory/notes?status=probation")[1]["notes"]] == ["n0001"]
    assert c.get("/api/memory/notes?status=retired")[1]["notes"] == []
    assert c.get("/api/memory/notes?status=bogus")[0] == 400


def test_memory_notes_missing_store(app_server):
    (app_server.repo / "memory" / "notes.jsonl").unlink()
    assert app_server.client.get("/api/memory/notes")[1]["notes"] == []


def test_retire_note(app_server):
    c = app_server.client
    assert c.post("/api/memory/notes/n0001/retire") == (200, {"retired": True})
    notes = {n["id"]: n["status"] for n in c.get("/api/memory/notes")[1]["notes"]}
    assert notes == {"n0001": "retired", "n0002": "active"}
    assert c.post("/api/memory/notes/n9999/retire")[0] == 404
    assert c.post("/api/memory/notes/..%2fx/retire")[0] in (400, 404)


def test_screen_unavailable_is_503(app_server):
    status, body = app_server.client.get("/api/screen.png")
    assert status == 503 and "error" in body


def test_screen_capture(tmp_path):
    running = start_server(tmp_path, capture=lambda display: png_bytes((1, 2, 3)) if display == ":99" else b"")
    try:
        status, body, headers = running.client.request("/api/screen.png")
        assert status == 200 and headers["Content-Type"] == "image/png" and body == png_bytes((1, 2, 3))
    finally:
        stop_server(running)


# ---- /api/live

def test_live_tails_the_newest_run(app_server):
    c = app_server.client
    status, body = c.get("/api/live")
    assert status == 200
    assert body["run_id"] == LIVE_RUN and body["kind"] == "game" and body["model"] == "claude-haiku-5-5"
    assert body["running"] is False  # no agent process
    assert body["step"] == 4 and body["cost_usd"] == pytest.approx(0.004)
    assert body["last"] == {"step": 4, "thought": "thought 4", "executed": [{"type": "click", "x": 4, "y": 1}],
                            "error": None, "state": {"state_type": "monster", "floor": 2}, "notes_shown": None,
                            "time": "2026-10-25T12:00:04+00:00"}
    assert body["screenshot"] == f"/api/runs/{LIVE_RUN}/screenshots/step_0004.png"
    assert c.request(body["screenshot"])[0] == 200

    run = app_server.repo / "runs" / LIVE_RUN
    with (run / "steps.jsonl").open("a") as f:
        f.write(json.dumps(step_record(5, cost=0.01, error="boom", notes_shown=["n0001"])) + "\n")
        f.write('{"step": 6, "thought": "half writ')  # still being written
    body = c.get("/api/live")[1]
    assert body["step"] == 5 and body["cost_usd"] == pytest.approx(0.014)
    assert body["last"]["error"] == "boom" and body["last"]["notes_shown"] == ["n0001"]
    assert body["screenshot"] is None  # step_0005.png not saved

    proc = fake_agent("python -m sts_cua.supervisor --display :99")
    try:
        time.sleep(0.2)
        assert c.get("/api/live")[1]["running"] is True
    finally:
        proc.kill()
        proc.wait()


def test_live_finished_run_uses_summary(app_server):
    run = app_server.repo / "runs" / LIVE_RUN
    (run / "summary.json").write_text(json.dumps({"steps": 4, "cost_usd": 0.5, "ended_at": "2026-10-25T18:00:00Z"}))
    body = app_server.client.get("/api/live")[1]
    assert body["step"] == 4 and body["cost_usd"] == 0.5 and body["running"] is False


def test_live_without_runs(app_server):
    import shutil

    shutil.rmtree(app_server.repo / "runs")
    assert app_server.client.get("/api/live") == (200, {
        "run_id": None, "kind": None, "model": None, "running": False, "step": 0, "cost_usd": 0.0,
        "last": None, "screenshot": None})


def test_tail_lines_reads_from_the_end(tmp_path):
    path = tmp_path / "big.jsonl"
    with path.open("w") as f:
        for i in range(20000):
            f.write(json.dumps({"step": i, "raw": "x" * 200}) + "\n")
    assert files.tail_lines(path, 3, block=1000) == [json.dumps({"step": i, "raw": "x" * 200})
                                                      for i in (19997, 19998, 19999)]
    assert files.last_record(path)["step"] == 19999
    assert files.tail_lines(tmp_path / "missing", 5) == []
    small = tmp_path / "small"
    small.write_text("a\nb")
    assert files.tail_lines(small, 10, block=1) == ["a", "b"]


def test_cost_tracker_reads_only_new_bytes(tmp_path):
    path = tmp_path / "steps.jsonl"
    path.write_text(json.dumps(step_record(1, 0.5)) + "\n")
    tracker = files.CostTracker()
    assert tracker.cost(path) == 0.5
    with path.open("a") as f:
        f.write(json.dumps(step_record(2, 0.25)) + "\n" + '{"usage": {"cost_usd": 9')
    assert tracker.cost(path) == 0.75
    with path.open("a") as f:
        f.write("}}\n")
    assert tracker.cost(path) == 9.75
    path.write_text(json.dumps(step_record(1, 0.125)) + "\n")  # replaced by a shorter file
    assert tracker.cost(path) == 0.125
