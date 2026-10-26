"""The app's Model server card: /api/models, /api/model/start|stop|download, and the shared process pattern."""

import re
import subprocess
import time
from pathlib import Path

import pytest
from test_serve import fake_download

from sts_cua import serve
from sts_cua.app.procs import MODEL_PATTERN
from sts_cua.models.registry import MODELS

STACK_DOWN = Path(__file__).parent.parent / "tools" / "stack_down.sh"


def test_models_lists_the_registry(app_server):
    fake_download(MODELS["ui-venus-2-9b"], app_server.models_dir)
    status, body = app_server.client.get("/api/models")
    assert status == 200
    rows = {r["name"]: r for r in body["models"]}
    assert set(rows) == set(MODELS)
    assert [r["rank"] for r in body["models"]] == sorted((m.rank for m in MODELS.values()), reverse=True)
    venus, holo = rows["ui-venus-2-9b"], rows["holo4-27b"]
    assert venus["downloaded"] and not holo["downloaded"]
    assert venus["fits"] == {"coop": True, "solo": True} and holo["fits"] == {"coop": False, "solo": True}
    assert holo["file_gib"] == round(MODELS["holo4-27b"].file_gib, 2) and holo["rank"] == 90
    assert venus["download"]["bytes"] == venus["download"]["total"]
    assert body["best"] == {"coop": "ui-venus-2-9b", "solo": "ui-venus-2-9b"}  # only one downloaded
    assert body["server"]["running"] is False and body["server"]["model"] is None
    assert body["gpu"]["free_mib"] == 20000


def test_model_start_allowlist_and_409(app_server):
    c, launcher = app_server.client, app_server.launcher
    for bad in ({}, {"model": "holo4-27b"}, {"model": "holo4-27b", "mode": "duo"},
                {"model": "holo4-27b; rm -rf ~", "mode": "solo"}, {"model": ["best"], "mode": "solo"},
                {"model": "holo4-27b", "mode": "solo", "force": "yes"}):
        assert c.post("/api/model/start", bad)[0] == 400, bad
    status, body = c.post("/api/model/start", {"model": "holo4-27b", "mode": "solo"})
    assert status == 409 and "isn't downloaded" in body["error"]
    fake_download(MODELS["holo4-27b"], app_server.models_dir)
    status, body = c.post("/api/model/start", {"model": "holo4-27b", "mode": "coop"})
    assert status == 409 and "coop budget" in body["error"] and "--force" in body["error"]
    assert launcher.calls == []

    status, body = c.post("/api/model/start", {"model": "holo4-27b", "mode": "solo"})
    assert status == 200 and body["model"] == "holo4-27b" and body["mode"] == "solo"
    d = app_server.models_dir / "Hcompany" / "Holo4-27B-GGUF"
    argv = launcher.argv()
    assert argv[0].endswith("/bin/llama-server")
    assert argv[1:5] == ["-m", str(d / "q4_k_m.gguf"), "--mmproj", str(d / "mmproj.f16.gguf")]
    assert argv[argv.index("--alias") + 1] == "holo4-27b" and argv[argv.index("--port") + 1] == "8000"
    assert c.get("/api/status")[1]["busy"] == "model_start"
    assert c.post("/api/model/start", {"model": "ui-venus-2-9b", "mode": "coop"})[0] == 409
    assert c.post("/api/vllm/start", {"model": "qwen3-vl-8b"})[0] == 409

    app_server.ready.append(True)
    assert c.get("/api/status")[1]["busy"] is None
    body = c.get("/api/models")[1]
    assert body["server"]["running"] and body["server"]["model"] == "holo4-27b" and body["server"]["mode"] == "solo"
    assert body["server"]["served"] == ["holo4-27b"] and body["server"]["ready"]
    assert [r["name"] for r in body["models"] if r["running"]] == ["holo4-27b"]
    deadline = time.monotonic() + 5  # the load-time watcher notices the server answering
    while "ready_s" not in (app_server.app.procs.managed("model") or {}) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (app_server.state_dir / "logs" / "model_loads.jsonl").exists()

    assert c.post("/api/model/stop")[1]["stopped"] is True
    launcher.procs[0].wait(timeout=5)
    assert not c.get("/api/status")[1]["processes"]["model"]["running"]


def test_model_start_best_and_vram_refusal(app_server):
    c, launcher = app_server.client, app_server.launcher
    for name in ("ui-venus-2-9b", "qwen3.8-27b", "holo4-27b"):
        fake_download(MODELS[name], app_server.models_dir)
    app_server.gpu["free_mib"] = 19 * 1024
    status, body = c.post("/api/model/start", {"model": "best", "mode": "coop"})
    assert status == 200 and body["model"] == "ui-venus-2-9b" and "best that fits" in body["reason"]
    c.post("/api/model/stop")
    launcher.procs[-1].wait(timeout=5)
    status, body = c.post("/api/model/start", {"model": "best", "mode": "solo"})
    assert status == 200 and body["model"] == "holo4-27b"
    c.post("/api/model/stop")
    launcher.procs[-1].wait(timeout=5)

    app_server.gpu["free_mib"] = 8 * 1024
    status, body = c.post("/api/model/start", {"model": "ui-venus-2-9b", "mode": "coop"})
    assert status == 409 and "only 8.0 GiB" in body["error"]
    status, body = c.post("/api/model/start", {"model": "best", "mode": "coop"})
    assert status == 409 and "no downloaded model fits" in body["error"]
    n = len(launcher.calls)
    status, body = c.post("/api/model/start", {"model": "ui-venus-2-9b", "mode": "coop", "force": True})
    assert status == 200 and len(launcher.calls) == n + 1


def test_model_download_runs_the_cli(app_server):
    c, launcher = app_server.client, app_server.launcher
    assert c.post("/api/model/download", {"model": "best"})[0] == 400
    assert c.post("/api/model/download", {"model": "../x"})[0] == 400
    status, body = c.post("/api/model/download", {"model": "holo4-27b"})
    assert status == 200 and body["model"] == "holo4-27b"
    assert launcher.argv() == ["/venv/bin/python", "-m", "sts_cua.serve", "--models-dir", str(app_server.models_dir),
                               "download", "holo4-27b"]
    assert c.post("/api/model/download", {"model": "ui-venus-2-9b"})[0] == 409
    body = c.get("/api/models")[1]
    assert body["download"] == {"running": True, "model": "holo4-27b"}
    assert next(r for r in body["models"] if r["name"] == "holo4-27b")["download"]["running"]
    assert c.get("/api/logs/download")[0] == 200 and c.get("/api/logs/model")[0] == 200


MODEL_SERVERS = [
    "/home/raph/llama.cpp/build/bin/llama-server -m /mnt/d/models/x.gguf --port 8000",
    "llama-server -m x.gguf",
    "/home/raph/sts-cua/.venv/bin/python3 /home/raph/sts-cua/.venv/bin/vllm serve Qwen/Qwen3-VL-8B-Instruct-FP8",
    "vllm serve Qwen/Qwen3-VL-8B-Instruct-FP8 --port 8000",
    "python -m vllm.entrypoints.openai.api_server --model x",
]
NOT_SERVERS = [
    "python -m sts_cua.serve start best --mode solo --llama-server /home/raph/llama.cpp/build/bin/llama-server",
    "python -m sts_cua.serve status",
    "bash tools/install_llama_cpp.sh",
    "vim /home/raph/llama.cpp/tools/server/llama-server.cpp",
    "python -m sts_cua.app --vllm /opt/vllm/bin/vllm",
    "pgrep -f " + MODEL_PATTERN,  # another thread's pgrep with the same pattern
]


def stack_down_pattern() -> str:
    line = next(line for line in STACK_DOWN.read_text().splitlines() if line.startswith('stop "local model server"'))
    return re.search(r"-f '([^']+)'", line).group(1)


def test_stack_down_uses_the_model_pattern():
    assert stack_down_pattern() == MODEL_PATTERN


@pytest.mark.parametrize("cmdline", MODEL_SERVERS + NOT_SERVERS)
def test_model_pattern(cmdline):
    found = subprocess.run(["grep", "-qE", MODEL_PATTERN], input=cmdline, text=True).returncode == 0
    assert found == (cmdline in MODEL_SERVERS)
    assert bool(re.search(MODEL_PATTERN, cmdline)) == found


def test_model_pattern_with_real_pgrep():
    procs = {c: subprocess.Popen(["bash", "-c", f'exec -a "{c}" sleep 30']) for c in MODEL_SERVERS[:2] + NOT_SERVERS[:2]}
    try:
        time.sleep(0.3)
        out = subprocess.run(["pgrep", "-f", MODEL_PATTERN], capture_output=True, text=True).stdout
        matched = {int(p) for p in out.split()}
        for cmdline, proc in procs.items():
            assert (proc.pid in matched) == (cmdline in MODEL_SERVERS), cmdline
    finally:
        for proc in procs.values():
            proc.kill()
            proc.wait()


def test_serve_uses_the_same_name_as_the_app():
    assert serve.NAME == "model"
