"""GET /api/evals and /api/evals/<name>: memory A/B experiments under runs/evals/."""

import json

import pytest

from sts_cua import eval as evaluation


def write_eval(repo, name, scores, created_at, seeds=("S1", "S2")):
    plan = {"name": name, "created_at": created_at, "mode": "paired" if seeds else "unpaired",
            "arms": [{"label": "none", "memory": None, "sha256": None},
                     {"label": "frozen:memory/frozen/notes-20261025.jsonl", "memory": None, "sha256": None}],
            "seeds": list(seeds), "runs_per_arm": 2, "character": "the Ironclad", "ascension": 0, "max_attempts": 2,
            "schedule": evaluation.schedule(["none", "frozen:memory/frozen/notes-20261025.jsonl"], list(seeds) or None, 2)}
    d = repo / "runs" / "evals" / name
    d.mkdir(parents=True)
    (d / "plan.json").write_text(json.dumps(plan))
    with (d / "results.jsonl").open("w") as f:
        for slot, score in zip(plan["schedule"], scores):
            f.write(json.dumps({**slot, "attempt": 1, "valid": True, "invalid_reason": None, "warnings": [],
                                "score": score, "floor": score, "outcome": "died", "cost_usd": 0.25}) + "\n")
    evaluation.write_report(d, plan, evaluation.load_results(d), stop_reason="budget")
    return d


def test_evals_list_and_detail(app_server):
    repo = app_server.repo
    write_eval(repo, "ab-old", [5, 8, 9, 6], "2026-10-24T10:00:00+00:00")
    write_eval(repo, "ab-new", [4, 7], "2026-10-25T10:00:00+00:00", seeds=())
    broken = repo / "runs" / "evals" / "broken"
    broken.mkdir()
    (broken / "plan.json").write_text("{not json")
    (repo / "runs" / "evals" / "no-plan").mkdir()
    c = app_server.client

    status, body = c.get("/api/evals")
    assert status == 200 and [e["name"] for e in body["evals"]] == ["ab-new", "ab-old"]
    old = body["evals"][1]
    assert old["mode"] == "paired" and old["valid_runs"] == 4 and old["planned_runs"] == 4 and old["complete"]
    assert old["cost_usd"] == 1.0 and old["stop_reason"] == "budget" and 0 <= old["p_value"] <= 1
    assert old["arms"] == ["none", "frozen:memory/frozen/notes-20261025.jsonl"]
    assert body["evals"][0]["valid_runs"] == 2 and not body["evals"][0]["complete"]

    status, body = c.get("/api/evals/ab-old")
    assert status == 200 and body["plan"]["name"] == "ab-old" and len(body["results"]) == 4
    assert body["report"]["test"]["test"] == "wilcoxon_signed_rank" and body["report"]["test"]["pairs"] == 2
    assert body["report"]["per_arm"]["none"]["mean_score"] == 5.5

    # The runs list is unaffected: runs/evals has no meta.json.
    assert all(r["run_id"] != "evals" for r in c.get("/api/runs")[1]["runs"])


def test_evals_empty(app_server):
    assert app_server.client.get("/api/evals") == (200, {"evals": []})


@pytest.mark.parametrize("path", [
    "/api/evals/missing", "/api/evals/no-plan", "/api/evals/..", "/api/evals/..%2f..%2fapp-token",
    "/api/evals/%2e%2e", "/api/evals/a/b", "/api/evals/../ledger",
])
def test_eval_detail_rejects_bad_names(app_server, path):
    (app_server.repo / "runs" / "evals" / "no-plan").mkdir(parents=True)
    status, body = app_server.client.get(path)
    assert status in (400, 404) and "error" in body


def test_evals_are_get_only(app_server):
    assert app_server.client.post("/api/evals")[0] == 405


def test_eval_run_counts_as_the_supervisor(app_server):
    import subprocess
    import time

    proc = subprocess.Popen(["bash", "-c", 'exec -a "python -m sts_cua.eval run ab1 --display :99" sleep 30'])
    report = subprocess.Popen(["bash", "-c", 'exec -a "python -m sts_cua.eval report ab1" sleep 30'])
    try:
        time.sleep(0.2)
        sup = app_server.client.get("/api/status")[1]["processes"]["supervisor"]
        assert sup["running"] and sup["pid"] == proc.pid
        assert app_server.client.post("/api/runs/start", {"model": "scripted"})[0] == 409
    finally:
        for p in (proc, report):
            p.kill()
            p.wait()
