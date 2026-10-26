"""The app's Bake-off tab and llama.cpp install: argv for every action, conflicts, review images, results."""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest
from appfakes import png_bytes
from test_app_api import fake_agent
from test_serve import fake_download

from sts_cua.app import bench as B
from sts_cua.app.procs import AGENT_PATTERN, BENCH_PATTERN, INSTALL_PATTERN
from sts_cua.app.routes import ApiError
from sts_cua.bench import __main__ as bench_cli
from sts_cua.models.registry import MODELS

PY = "/venv/bin/python"
LOG = Path("/state/logs/model.log")
STACK_DOWN = Path(__file__).parent.parent / "tools" / "stack_down.sh"


def argv(action, body=None):
    return B.bench_argv(action, body or {}, PY, LOG)[0]


def settings(action, body=None):
    return B.bench_argv(action, body or {}, PY, LOG)[1]


def wait_for(fn, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not fn():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.05)


# ---- argv building


def test_argv_for_every_action():
    base = [PY, "-m", "sts_cua.bench"]
    assert argv("extract") == base + ["extract"]
    assert argv("extract", {"max_per_run": 5}) == base + ["extract", "--max-per-run", "5"]
    assert argv("review") == base + ["review"]
    assert argv("delete", {"ids": ["20261025T181542Z-0042", "20261025T181542Z-0042", "a.b-1"]}) == \
        base + ["review", "--delete", "20261025T181542Z-0042", "a.b-1"]
    assert argv("grounding", {"models": ["claude-haiku-5-5"]}) == base + ["grounding", "claude-haiku-5-5", "--mode", "solo"]
    assert argv("grounding", {"models": ["holo4-27b", "ui-venus-2-9b", "holo4-27b"], "mode": "coop", "serve": True,
                              "limit": 30}) == \
        base + ["grounding", "holo4-27b", "ui-venus-2-9b", "--mode", "coop", "--serve", "--limit", "30"]
    assert argv("baseline") == base + ["baseline", "--label", "pre-server"]
    assert argv("baseline", {"label": "desktop", "mode": "coop"}) == base + ["baseline", "--label", "desktop", "--mode", "coop"]
    assert argv("resources", {"model": "holo4-27b", "mode": "solo", "game": True}) == \
        base + ["resources", "holo4-27b", "--mode", "solo", "--game"]
    assert argv("resources", {"model": "qwen3-vl-8b", "mode": "coop", "serve": True}) == \
        base + ["resources", "qwen3-vl-8b", "--mode", "coop", "--serve", "--server-log", str(LOG)]  # vLLM's log
    assert argv("clicks", {"model": "ui-venus-2-9b"}) == base + ["clicks", "ui-venus-2-9b", "--xvfb", "--display", ":98",
                                                                 "--n", "20"]
    assert argv("clicks", {"model": "claude-haiku-5-5", "n": 500})[-1] == "100"
    assert argv("write_docs") == base + ["report", "--write-docs", "--docs", "docs/hosting.md"]


def test_settings_say_what_needs_a_server():
    assert settings("grounding", {"models": ["claude-haiku-5-5", "holo4-27b"]})["needs_server"] == ["holo4-27b"]
    s = settings("grounding", {"models": ["claude-haiku-5-5", "holo4-27b"], "serve": True})
    assert s["serve"] is True and s["needs_server"] == []
    assert settings("grounding", {"models": ["claude-haiku-5-5"], "serve": True})["serve"] is False  # nothing to serve
    assert settings("resources", {"model": "holo4-27b", "mode": "solo"})["needs_server"] == ["holo4-27b"]
    assert settings("clicks", {"model": "claude-sonnet-5-5"})["needs_server"] == []


@pytest.mark.parametrize("action,body", [
    ("extract", {"max_per_run": "5"}), ("extract", {"max_per_run": 1.5}),
    ("delete", {}), ("delete", {"ids": []}), ("delete", {"ids": "a"}), ("delete", {"ids": ["--columns"]}),
    ("delete", {"ids": ["../x"]}), ("delete", {"ids": ["a b"]}), ("delete", {"ids": [7]}),
    ("delete", {"ids": ["x"] * 501}),
    ("grounding", {}), ("grounding", {"models": "holo4-27b"}), ("grounding", {"models": []}),
    ("grounding", {"models": ["claude-opus-5-5"]}), ("grounding", {"models": ["holo4-27b; rm -rf ~"]}),
    ("grounding", {"models": ["scripted"]}), ("grounding", {"models": ["holo4-27b"], "mode": "duo"}),
    ("grounding", {"models": ["holo4-27b"], "serve": "yes"}), ("grounding", {"models": ["holo4-27b"], "limit": "3"}),
    ("baseline", {"label": "after"}), ("baseline", {"mode": "x"}),
    ("resources", {"model": "claude-haiku-5-5", "mode": "solo"}), ("resources", {"model": "holo4-27b"}),
    ("resources", {"model": "holo4-27b", "mode": "solo", "game": 1}),
    ("clicks", {}), ("clicks", {"model": "holo4-27b", "n": "20"}),
])
def test_argv_validation(action, body):
    with pytest.raises(ApiError) as e:
        argv(action, body)
    assert e.value.status == 400


def test_grounding_model_cap():
    names = [*MODELS, "claude-haiku-5-5", "claude-sonnet-5-5"]
    if len(names) > B.MAX_MODELS:
        with pytest.raises(ApiError):
            argv("grounding", {"models": names})
    assert argv("grounding", {"models": names[:B.MAX_MODELS]})[4:4 + B.MAX_MODELS] == names[:B.MAX_MODELS]


def test_every_argv_parses_with_the_bench_cli(monkeypatch):
    """The real `python -m sts_cua.bench` parser takes every argv the app builds (the command itself is faked)."""
    seen = []
    for name in ("extract", "review", "grounding", "baseline", "resources", "clicks", "report"):
        monkeypatch.setattr(bench_cli, f"cmd_{name}", lambda args, name=name: seen.append((name, args)))
    cases = [("extract", {"max_per_run": 3}), ("delete", {"ids": ["r-0001"]}),
             ("grounding", {"models": ["holo4-27b", "claude-haiku-5-5"], "mode": "max", "serve": True, "limit": 4}),
             ("baseline", {"label": "desktop"}), ("resources", {"model": "qwen3-vl-8b", "mode": "coop", "game": True}),
             ("clicks", {"model": "ui-venus-2-9b", "n": 5}), ("write_docs", {})]
    for action, body in cases:
        bench_cli.main(argv(action, body)[3:])
    got = {name: args for name, args in seen}
    assert got["extract"].max_per_run == 3
    assert got["review"].delete == ["r-0001"]
    g = got["grounding"]
    assert (g.models, g.mode, g.serve, g.limit) == (["holo4-27b", "claude-haiku-5-5"], "max", True, 4)
    assert (got["baseline"].label, got["baseline"].mode) == ("desktop", None)
    r = got["resources"]
    assert (r.model_name, r.mode, r.game, r.serve, r.server_log) == ("qwen3-vl-8b", "coop", True, False, str(LOG))
    c = got["clicks"]
    assert (c.model_name, c.n, c.xvfb, c.display) == ("ui-venus-2-9b", 5, True, ":98")
    assert got["report"].write_docs and got["report"].docs == "docs/hosting.md"


def test_install_argv():
    script = Path("/repo/tools/install_llama_cpp.sh")
    assert B.install_argv({}, script)[0] == ["bash", str(script), "--build"]
    assert B.install_argv({"prebuilt": True}, script)[0] == ["bash", str(script), "--prebuilt"]
    assert B.install_argv({"prebuilt": True, "tag": "b11900"}, script)[0] == ["bash", str(script), "--prebuilt", "b11900"]
    for bad in ({"prebuilt": "yes"}, {"tag": "b11900"}, {"prebuilt": True, "tag": "latest; rm -rf ~"},
                {"prebuilt": True, "tag": "--build"}, {"prebuilt": True, "tag": 11900}):
        with pytest.raises(ApiError) as e:
            B.install_argv(bad, script)
        assert e.value.status == 400, bad


# ---- endpoints


def test_grounding_job_runs_once_and_stops(app_server):
    c, launcher = app_server.client, app_server.launcher
    assert c.post("/api/bench/grounding", {"models": "x"})[0] == 400
    status, body = c.post("/api/bench/grounding", {"models": ["claude-haiku-5-5"], "limit": 2})
    assert status == 200 and body["action"] == "grounding"
    call = launcher.calls[-1]
    assert call["argv"] == [PY, "-m", "sts_cua.bench", "grounding", "claude-haiku-5-5", "--mode", "solo", "--limit", "2"]
    assert call["cwd"] == app_server.repo and call["env"]["PYTHONUNBUFFERED"] == "1"
    assert call["env"]["STS_CUA_MODELS"] == str(app_server.models_dir)
    assert call["env"]["LLAMA_SERVER"].endswith("/bin/llama-server")
    assert (app_server.state_dir / "logs" / "bench.log").read_text().startswith("--- ")

    st = c.get("/api/status")[1]
    assert st["bench"]["running"] and st["bench"]["pid"] == body["pid"] and st["busy"] == "bench"
    for action, b in (("extract", {}), ("review", {}), ("baseline", {}), ("write-docs", {}),
                      ("clicks", {"model": "claude-haiku-5-5"})):
        status, err = c.post(f"/api/bench/{action}", b)
        assert status == 409 and "already running (grounding)" in err["error"], action

    with (app_server.state_dir / "logs" / "bench.log").open("a") as f:
        f.write("claude-haiku-5-5 1/2 r-0001: hit 3.0 px 1.20 s\n")
    job = c.get("/api/bench")[1]
    assert job["job"]["running"] and job["job"]["action"] == "grounding" and not job["job"]["external"]
    assert job["last"]["running"] and job["last"]["output"][-1].startswith("claude-haiku-5-5 1/2")
    assert c.get("/api/logs/bench")[1]["lines"][-1].endswith("1.20 s")

    stopped = c.post("/api/bench/stop")[1]
    assert stopped["pids"] == [body["pid"]]
    launcher.procs[-1].wait(timeout=5)
    last = c.get("/api/bench")[1]["last"]
    assert last["running"] is False and last["exit_code"] == -signal.SIGINT
    assert c.get("/api/status")[1]["busy"] is None
    assert c.post("/api/bench/extract", {"max_per_run": 2})[0] == 200


def test_external_bench_counts(app_server):
    proc = fake_agent("python -m sts_cua.bench grounding holo4-27b --serve")
    try:
        wait_for(lambda: app_server.client.get("/api/status")[1]["bench"]["running"])
        assert app_server.client.post("/api/bench/review")[0] == 409
        job = app_server.client.get("/api/bench")[1]["job"]
        assert job["external"] and job["action"] == "external"
        assert app_server.client.post("/api/model/start", {"model": "best", "mode": "solo"})[0] == 409
    finally:
        proc.kill()
        proc.wait()


def _start_holo(app_server):
    fake_download(MODELS["holo4-27b"], app_server.models_dir)
    assert app_server.client.post("/api/model/start", {"model": "holo4-27b", "mode": "solo"})[0] == 200


def test_local_models_need_the_right_server(app_server):
    c = app_server.client
    status, body = c.post("/api/bench/grounding", {"models": ["holo4-27b"]})
    assert status == 409 and "no model server is running" in body["error"] and "start each model" in body["error"]
    status, body = c.post("/api/bench/grounding", {"models": ["holo4-27b", "ui-venus-2-9b"]})
    assert status == 409 and "only one model server" in body["error"]
    assert c.post("/api/bench/clicks", {"model": "holo4-27b"})[0] == 409
    assert c.post("/api/bench/resources", {"model": "holo4-27b", "mode": "solo"})[0] == 409

    _start_holo(app_server)
    app_server.ready.append(True)
    status, body = c.post("/api/bench/grounding", {"models": ["ui-venus-2-9b"]})
    assert status == 409 and "serving holo4-27b, not ui-venus-2-9b" in body["error"]
    status, body = c.post("/api/bench/resources", {"model": "holo4-27b", "mode": "solo", "serve": True})
    assert status == 409 and "already running" in body["error"]
    status, body = c.post("/api/bench/baseline", {"label": "pre-server"})
    assert status == 409 and "no model server" in body["error"]
    status, body = c.post("/api/bench/resources", {"model": "holo4-27b", "mode": "solo", "game": True})
    assert status == 200 and body["argv"][-1] == "--game"


def test_serving_jobs_and_model_start_block_each_other(app_server):
    c = app_server.client
    _start_holo(app_server)  # still loading: app_server.ready is empty
    status, body = c.post("/api/bench/grounding", {"models": ["holo4-27b", "ui-venus-2-9b"], "serve": True})
    assert status == 409 and "starting" in body["error"]
    app_server.ready.append(True)
    c.post("/api/model/stop")
    app_server.launcher.procs[-1].wait(timeout=5)

    status, body = c.post("/api/bench/grounding", {"models": ["holo4-27b", "ui-venus-2-9b"], "serve": True,
                                                   "mode": "coop"})
    assert status == 200 and "--serve" in body["argv"]
    status, body = c.post("/api/model/start", {"model": "holo4-27b", "mode": "solo"})
    assert status == 409 and "bench job" in body["error"]
    status, body = c.post("/api/llama/install", {})
    assert status == 409 and "bench job" in body["error"]


def test_serving_waits_for_an_agent_on_a_local_model(app_server):
    c = app_server.client
    assert c.post("/api/runs/start", {"model": "holo4-27b"})[0] == 200
    status, body = c.post("/api/bench/grounding", {"models": ["holo4-27b"], "serve": True})
    assert status == 409 and "supervisor" in body["error"]
    assert c.post("/api/bench/grounding", {"models": ["claude-haiku-5-5"], "serve": True})[0] == 200  # nothing served
    c.post("/api/bench/stop")
    c.post("/api/runs/stop")
    for p in app_server.launcher.procs:
        p.wait(timeout=5)
    assert c.post("/api/runs/start", {"model": "claude-haiku-5-5"})[0] == 200
    assert c.post("/api/bench/grounding", {"models": ["holo4-27b"], "serve": True})[0] == 200


def test_desktop_baseline_refused_with_the_game_up(app_server, tmp_path):
    game = tmp_path / "SlayTheSpire2"  # pgrep -x matches the process name, so a sleep binary of that name
    shutil.copy(shutil.which("sleep"), game)
    proc = subprocess.Popen([str(game), "30"])
    try:
        wait_for(lambda: app_server.client.get("/api/status")[1]["processes"]["game"]["running"])
        status, body = app_server.client.post("/api/bench/baseline", {"label": "desktop"})
        assert status == 409 and "desktop baseline" in body["error"]
        assert app_server.client.post("/api/bench/baseline", {"label": "pre-server", "mode": "coop"})[0] == 200
    finally:
        proc.kill()
        proc.wait()


def write_targets(repo: Path, ids: list[str]) -> Path:
    d = repo / "bench"
    (d / "images").mkdir(parents=True, exist_ok=True)
    with (d / "targets.jsonl").open("w") as f:
        for i in ids:
            f.write(json.dumps({"id": i, "image": f"images/{i}.png", "instruction": f"Click <b>{i}</b>", "x": 1, "y": 2,
                                "radius": 40, "size": [1280, 720], "state_type": "monster", "check": None}) + "\n")
    (d / "rejected.txt").write_text("old-1\nold-2\n")
    return d


def test_targets_sheets_and_delete(app_server, tmp_path):
    c, repo = app_server.client, app_server.repo
    empty = c.get("/api/bench")[1]["targets"]
    assert empty["count"] == 0 and empty["sheets"] == [] and empty["error"] is None
    d = write_targets(repo, ["r1-0001", "r1-0002", "r2-0003"])
    (d / "review").mkdir()
    (d / "review" / "sheet-01.png").write_bytes(png_bytes((1, 2, 3)))
    os.utime(d / "review" / "sheet-01.png", (1, 1))  # older than the targets
    (d / "review" / "notes.png").write_bytes(png_bytes())
    t = c.get("/api/bench")[1]["targets"]
    assert t["count"] == 3 and t["rejected"] == 2 and t["sheets_stale"]
    assert [i["id"] for i in t["items"]] == ["r1-0001", "r1-0002", "r2-0003"]
    assert t["items"][0]["instruction"] == "Click <b>r1-0001</b>"
    assert [s["name"] for s in t["sheets"]] == ["sheet-01.png"]
    assert re.fullmatch(r"/api/bench/review/sheet-01\.png\?v=\d+", t["sheets"][0]["url"])

    status, body, headers = c.request("/api/bench/review/sheet-01.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body == png_bytes((1, 2, 3))
    assert c.get("/api/bench/review/sheet-01.png", token=None)[0] == 401

    status, body = c.post("/api/bench/delete", {"ids": ["r1-0002", "nope-1"]})
    assert status == 400 and "nope-1" in body["error"]
    status, body = c.post("/api/bench/delete", {"ids": ["r1-0002"]})
    assert status == 200 and body["argv"][3:] == ["review", "--delete", "r1-0002"]


def test_review_images_are_path_checked(app_server, tmp_path):
    c = app_server.client
    review = app_server.repo / "bench" / "review"
    review.mkdir(parents=True)
    (review / "sheet-02.png").symlink_to(tmp_path / "outside.png")  # appfakes puts a png there
    (review / "sheet-03.png").write_text("x")
    for path in ("/api/bench/review/sheet-02.png", "/api/bench/review/notes.png", "/api/bench/review/sheet-01.png",
                 "/api/bench/review/sheet-03.txt", "/api/bench/review/..", "/api/bench/review/%2e%2e%2fx.png",
                 "/api/bench/review/sheet-01.png/x", "/api/bench/review/.."):
        assert c.get(path)[0] in (400, 404), path
    assert c.get("/api/bench/review/sheet-03.png")[0] == 200
    assert c.post("/api/bench/review/sheet-03.png")[0] in (404, 405)


def write_result(repo: Path, name: str, data) -> None:
    d = repo / "runs" / "bench" / name
    d.mkdir(parents=True)
    (d / "results.json").write_text(data if isinstance(data, str) else json.dumps(data))


def summary(hits, answered, p50, **extra):
    return {"targets": answered, "answered": answered, "errors": 0, "hits": hits, "malformed": 0,
            "hit_rate": hits / answered, "median_error_px": 12.0, "latency_p50_s": p50, "latency_p95_s": p50 * 2,
            "tokens_per_s": 40.0, **extra}


def test_results_parsing(app_server):
    c, repo = app_server.client, app_server.repo
    empty = c.get("/api/bench/results")[1]
    assert empty["grounding"] == [] and empty["ranking"] == [] and "No grounding results yet" in empty["markdown"]
    write_result(repo, "20261026T100000Z", {"kind": "grounding", "created_at": "2026-10-26T10:00:00+00:00",
                                            "game_build": "v0.107.1", "targets": 40, "runs": 3,
                                            "models": [{"model": "holo4-27b", "summary": summary(20, 40, 3.0)},
                                                       {"model": "claude-haiku-5-5", "summary": summary(36, 40, 2.0)}]})
    write_result(repo, "20261026T110000Z", {"kind": "grounding", "created_at": "2026-10-26T11:00:00+00:00",
                                            "game_build": "v0.107.1", "targets": 40, "runs": 3,
                                            "models": [{"model": "holo4-27b", "summary": summary(30, 40, 4.0)},
                                                       {"model": "ui-venus-2-9b", "summary": summary(30, 40, 1.5)},
                                                       {"bad": "entry"}]})
    write_result(repo, "20261026T120000Z", {"kind": "clicks", "created_at": "2026-10-26T12:00:00+00:00",
                                            "model": "ui-venus-2-9b", "summary": summary(18, 20, 1.1)})
    write_result(repo, "20261026T130000Z", {"kind": "resources", "created_at": "2026-10-26T13:00:00+00:00",
                                            "model": "holo4-27b", "mode": "coop", "vram_used_gib": 21.0,
                                            "vram_total_gib": 23.99, "model_vram_gib": 17.6, "ram_used_gib": 9.0,
                                            "server": {"rss_gib": 1.2}, "game": {"running": True, "rss_gib": 3.4}})
    write_result(repo, "20261026T140000Z", "{not json")
    write_result(repo, "20261026T150000Z", {"kind": "resources", "model": "x", "mode": "duo"})
    write_result(repo, "20261026T160000Z", ["a", "list"])

    status, body = c.get("/api/bench/results")
    assert status == 200
    rows = {r["model"]: r for r in body["grounding"]}
    assert set(rows) == {"holo4-27b", "claude-haiku-5-5", "ui-venus-2-9b"}
    assert rows["holo4-27b"]["hits"] == 30 and rows["holo4-27b"]["dir"] == "20261026T110000Z"  # newest wins
    assert rows["claude-haiku-5-5"]["local"] is False and rows["holo4-27b"]["local"] is True
    assert [r["model"] for r in body["ranking"]] == ["claude-haiku-5-5", "ui-venus-2-9b", "holo4-27b"]
    holo = next(r for r in body["ranking"] if r["model"] == "holo4-27b")
    assert holo["fits"]["coop"] == {"fits": False, "need_gib": 17.6, "how": "measured"}
    assert next(r for r in body["ranking"] if r["model"] == "claude-haiku-5-5")["fits"]["solo"]["how"] == "api"
    assert body["recommended"] == {"coop": "ui-venus-2-9b", "solo": "ui-venus-2-9b"}
    assert body["clicks"][0]["model"] == "ui-venus-2-9b" and body["clicks"][0]["hits"] == 18
    res = body["resources"]
    assert len(res) == 1 and res[0]["mode"] == "coop" and res[0]["fits"] is False and res[0]["budget_gib"] == 13.0
    assert res[0]["game_rss_gib"] == 3.4 and res[0]["server_rss_gib"] == 1.2
    assert "| holo4-27b | 2026-10-26 | 75% (30/40)" in body["markdown"] and "Recommended" in body["markdown"]


def test_write_docs_shows_what_changed(app_server):
    c, repo, state = app_server.client, app_server.repo, app_server.state_dir
    docs = repo / "docs" / "hosting.md"
    docs.parent.mkdir()
    docs.write_text("# Hosting\n\n- The vLLM process itself: **measure**\n")
    status, body = c.post("/api/bench/write-docs")
    assert status == 200 and body["argv"][3:] == ["report", "--write-docs", "--docs", "docs/hosting.md"]
    assert c.get("/api/bench")[1]["last"]["changed"] is None  # nothing reported yet
    # What the job would do: rewrite a row and print the rows it touched.
    docs.write_text("# Hosting\n\n- The vLLM process itself: 2.1 GiB RSS <!-- bench:ram_server -->\n")
    with (state / "logs" / "bench.log").open("a") as f:
        f.write("updated docs/hosting.md: the bench block, and measure rows: ram_server\n")
    app_server.launcher.kill_all()
    last = c.get("/api/bench")[1]["last"]
    assert last["action"] == "write_docs" and not last["running"]
    assert last["changed"]["rows"] == ["ram_server"]
    assert last["changed"]["diff"] == ["-- The vLLM process itself: **measure**",
                                       "+- The vLLM process itself: 2.1 GiB RSS <!-- bench:ram_server -->"]


def test_bench_state(app_server):
    c = app_server.client
    (app_server.repo / "runs" / "bench").mkdir(parents=True)
    (app_server.repo / "runs" / "bench" / "baseline.json").write_text(json.dumps({
        "pre-server": {"time": "2026-10-26T09:00:00+00:00", "gpu": {"used_mib": 6000}, "ram": {"used_mib": 9000},
                       "mode": "coop"}}))
    body = c.get("/api/bench")[1]
    assert body["job"]["running"] is False and body["last"] is None
    assert [m["name"] for m in body["models"]["local"]] == sorted(MODELS, key=lambda n: -MODELS[n].rank)
    assert body["models"]["reference"] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert body["modes"] == ["coop", "solo", "max"] and body["clicks_display"] == ":98"
    assert body["baselines"]["pre-server"] == {"time": "2026-10-26T09:00:00+00:00", "mode": "coop",
                                               "gpu_used_mib": 6000, "ram_used_mib": 9000}
    assert body["server"] == {"running": False, "model": None, "mode": None}
    assert c.post("/api/bench/nothing")[0] == 404
    assert c.get("/api/bench/grounding")[0] == 405


# ---- llama.cpp install


def test_llama_status_and_install(app_server):
    c, launcher, state = app_server.client, app_server.launcher, app_server.state_dir
    body = c.get("/api/llama")[1]
    assert body["found"] and body["path"].endswith("/bin/llama-server") and body["version"] == "9999 (fake)"
    assert body["install"]["running"] is False and body["install"]["tail"] == []

    assert c.post("/api/llama/install", {"tag": "b1"})[0] == 400
    status, body = c.post("/api/llama/install", {"prebuilt": True, "tag": "b11900"})
    assert status == 200
    assert launcher.argv() == ["bash", str(app_server.repo / "tools" / "install_llama_cpp.sh"), "--prebuilt", "b11900"]
    assert launcher.calls[-1]["cwd"] == app_server.repo
    assert c.post("/api/llama/install", {})[0] == 409
    status, err = c.post("/api/model/start", {"model": "best", "mode": "solo"})
    assert status == 409 and "llama.cpp" in err["error"]
    assert c.get("/api/status")[1]["busy"] == "install"
    assert c.get("/api/status")[1]["install"]["running"]

    with (state / "logs" / "install.log").open("a") as f:
        f.write("[install_llama_cpp] nvcc not found. Install the CUDA toolkit for WSL-Ubuntu:\n"
                "[install_llama_cpp] or run this script with --prebuilt, which needs no toolkit.\n")
    inst = c.get("/api/llama")[1]["install"]
    assert inst["running"] and inst["prebuilt"] is True and inst["nvcc_missing"]
    assert inst["tail"][-1].endswith("needs no toolkit.")
    assert c.get("/api/logs/install")[1]["lines"][-1].endswith("needs no toolkit.")
    launcher.kill_all()
    inst = c.get("/api/llama")[1]["install"]
    assert not inst["running"] and inst["exit_code"] == -signal.SIGKILL


def test_install_waits_for_the_model_server(app_server):
    _start_holo(app_server)
    status, body = app_server.client.post("/api/llama/install", {})
    assert status == 409 and "model server" in body["error"]


def test_llama_missing(app_server, monkeypatch):
    app_server.app.config.llama_server = None
    monkeypatch.setenv("HOME", str(app_server.repo))  # no ~/llama.cpp
    monkeypatch.setenv("PATH", "/nonexistent")
    body = app_server.client.get("/api/llama")[1]
    assert body == {**body, "found": False, "path": None, "version": None}


# ---- stack down


def test_stack_down_stops_bench_and_install(app_server):
    c, launcher = app_server.client, app_server.launcher
    assert c.post("/api/bench/grounding", {"models": ["claude-haiku-5-5"]})[0] == 200
    assert c.post("/api/llama/install", {"prebuilt": True})[0] == 200
    bench_proc, install_proc = launcher.procs[-2:]
    assert c.post("/api/stack/down")[0] == 200
    assert launcher.argv() == ["bash", str(app_server.repo / "tools" / "stack_down.sh")]
    assert bench_proc.wait(timeout=5) == -signal.SIGINT
    assert install_proc.wait(timeout=5) == -signal.SIGTERM
    assert c.post("/api/bench/review")[0] == 409  # while stack_down.sh runs
    assert c.post("/api/llama/install")[0] == 409


BENCH_RUNS = [
    "python -m sts_cua.bench grounding holo4-27b --serve",
    "/home/raph/sts-cua/.venv/bin/python -m sts_cua.bench report --write-docs",
    "python3.12 -m sts_cua.bench clicks ui-venus-2-9b --xvfb --display :98",
]
NOT_BENCH = ["python -m sts_cua.app", "python -m sts_cua.benchmark", "vim src/sts_cua/bench/report.py",
             "python -m sts_cua.serve start holo4-27b --mode solo"]
INSTALLS = ["bash /home/raph/sts-cua/tools/install_llama_cpp.sh --prebuilt", "/bin/bash tools/install_llama_cpp.sh",
            "sh install_llama_cpp.sh --build"]
NOT_INSTALLS = ["vim tools/install_llama_cpp.sh", "less /home/raph/sts-cua/tools/install_llama_cpp.sh",
                "bash tools/install_llama_cpp.sh.bak", "python -m sts_cua.app", "bash tools/stack_down.sh"]


def stack_down_line(prefix: str) -> str:
    line = next(line for line in STACK_DOWN.read_text().splitlines() if line.startswith(prefix))
    return re.search(r"-f '([^']+)'", line).group(1)


def test_stack_down_uses_the_shared_patterns():
    assert stack_down_line('stop "agent runs"') == AGENT_PATTERN
    assert stack_down_line("for pid in $(pgrep") == INSTALL_PATTERN


@pytest.mark.parametrize("cmdline", BENCH_RUNS + NOT_BENCH)
def test_bench_is_an_agent_run_but_not_the_app(cmdline):
    for pattern in (AGENT_PATTERN, BENCH_PATTERN):
        found = subprocess.run(["grep", "-qE", pattern], input=cmdline, text=True).returncode == 0
        assert found == (cmdline in BENCH_RUNS) == bool(re.search(pattern, cmdline)), pattern


@pytest.mark.parametrize("cmdline", INSTALLS + NOT_INSTALLS)
def test_install_pattern(cmdline):
    found = subprocess.run(["grep", "-qE", INSTALL_PATTERN], input=cmdline, text=True).returncode == 0
    assert found == (cmdline in INSTALLS) == bool(re.search(INSTALL_PATTERN, cmdline))


def test_patterns_with_real_pgrep():
    cmdlines = BENCH_RUNS[:2] + INSTALLS[:2] + NOT_BENCH[:1] + NOT_INSTALLS[:1]
    procs = {c: subprocess.Popen(["bash", "-c", f'exec -a "{c}" sleep 30']) for c in cmdlines}
    try:
        time.sleep(0.3)
        for pattern, want in ((AGENT_PATTERN, BENCH_RUNS), (BENCH_PATTERN, BENCH_RUNS), (INSTALL_PATTERN, INSTALLS)):
            out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
            matched = {int(p) for p in out.split()}
            for cmdline, proc in procs.items():
                assert (proc.pid in matched) == (cmdline in want), (pattern, cmdline)
    finally:
        for proc in procs.values():
            proc.kill()
            proc.wait()
