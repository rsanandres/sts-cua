"""python -m sts_cua.serve: argv per model, the VRAM check, downloads, status. No GPU, no network."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sts_cua import serve
from sts_cua.app.procs import ProcessManager
from sts_cua.models.registry import GIB, MODELS, budget

LLAMA = "/opt/llama/llama-server"
HELP_NEW = "-lm, --load-mode MODE   model loading mode"
HELP_OLD = "--no-mmap   do not memory-map model"


def fake_download(m, root):
    """Sparse files of exactly the registry's sizes."""
    d = serve.model_dir(m, root)
    if m.server == "vllm":
        d = d / "snapshots" / "abc123"
    d.mkdir(parents=True, exist_ok=True)
    for f in serve.files_of(m):
        with (d / f.name).open("wb") as out:
            out.truncate(f.size)


def test_llama_argv_per_model(tmp_path):
    for m in MODELS.values():
        if m.server != "llama":
            continue
        d = tmp_path / m.repo
        assert serve.server_argv(m, LLAMA, tmp_path, help_text=HELP_NEW) == [
            LLAMA, "-m", str(d / m.weights[0].name), "--mmproj", str(d / m.mmproj.name), "-c", "8192",
            "-ngl", "999", "-fa", "on", "--jinja", "-np", "1", "--host", "127.0.0.1", "--port", "8000",
            "--alias", m.served_name, *m.extra_args]
    holo = serve.server_argv(MODELS["holo4-27b"], LLAMA, tmp_path, port=8001, help_text="")
    assert holo[holo.index("--port") + 1] == "8001" and holo[holo.index("--alias") + 1] == "holo4-27b"


def test_vllm_argv():
    assert serve.server_argv(MODELS["qwen3-vl-8b"], "vllm") == [
        "vllm", "serve", "Qwen/Qwen3-VL-8B-Instruct-FP8", "--gpu-memory-utilization", "0.55", "--max-model-len",
        "8192", "--max-num-seqs", "1", "--limit-mm-per-prompt", '{"image":1,"video":0}', "--host", "127.0.0.1",
        "--port", "8000"]


def test_windows_drive_loads_without_mmap():
    m = MODELS["holo4-27b"]
    assert serve.on_drvfs(Path("/mnt/d/models")) and serve.on_drvfs(Path("/mnt/c/x"))
    assert not serve.on_drvfs(Path("/home/raph/models")) and not serve.on_drvfs(Path("/mnt/wslg/x"))
    assert serve.server_argv(m, LLAMA, Path("/mnt/d/models"), help_text=HELP_NEW)[-2:] == ["--load-mode", "none"]
    assert serve.server_argv(m, LLAMA, Path("/mnt/d/models"), help_text=HELP_OLD)[-1] == "--no-mmap"
    assert "--load-mode" not in serve.server_argv(m, LLAMA, Path("/home/raph/models"), help_text=HELP_NEW)


def test_models_dir_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    windows = tmp_path / "d-models"
    assert serve.models_root(None, windows) == tmp_path / "home" / "models"  # neither exists: the default
    windows.mkdir()
    assert serve.models_root(None, windows) == windows  # only the Windows drive has one
    (tmp_path / "home" / "models").mkdir(parents=True)
    assert serve.models_root(None, windows) == tmp_path / "home" / "models"
    monkeypatch.setenv("STS_CUA_MODELS", str(tmp_path / "env"))
    assert serve.models_root(None, windows) == tmp_path / "env"
    assert serve.models_root(tmp_path / "flag", windows) == tmp_path / "flag"


def test_models_dir_follows_the_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("STS_CUA_MODELS", raising=False)
    home, windows = tmp_path / "home" / "models", tmp_path / "d-models"
    home.mkdir(parents=True)
    (windows / MODELS["holo4-27b"].repo).mkdir(parents=True)
    assert serve.models_root(None, windows) == windows  # an empty ~/models doesn't hide D:
    (home / MODELS["ui-venus-2-9b"].repo).mkdir(parents=True)
    assert serve.models_root(None, windows) == home  # both have models: ~/models first


def test_choose_explicit_vram_refusal_and_force():
    have = set(MODELS)
    m, reason = serve.choose("holo4-27b", "solo", 21.0, have)
    assert m.name == "holo4-27b" and "rank 90" in reason
    with pytest.raises(serve.ServeError, match="only 17.0 GiB"):
        serve.choose("holo4-27b", "solo", 17.0, have)
    with pytest.raises(serve.ServeError, match="over the coop budget"):
        serve.choose("holo4-27b", "coop", 22.0, have)  # free now, but Raph's copy needs its share
    with pytest.raises(serve.ServeError, match="can't read free VRAM"):
        serve.choose("ui-venus-2-9b", "coop", None, have)
    m, reason = serve.choose("holo4-27b", "solo", 17.0, have, force=True)
    assert m.name == "holo4-27b" and "forced past" in reason
    with pytest.raises(serve.ServeError, match="isn't downloaded"):
        serve.choose("holo4-27b", "solo", 21.0, set(), force=True)
    assert serve.choose("qwen3-vl-8b", "coop", 15.0, set())[0].name == "qwen3-vl-8b"  # vLLM fetches it itself
    for bad in (("gpt-6", "solo"), ("holo4-27b", "duo")):
        with pytest.raises(serve.ServeError):
            serve.choose(*bad, 21.0, have)


def test_choose_best():
    have = set(MODELS)
    assert serve.choose("best", "solo", 23.0, have)[0].name == "holo4-27b"
    assert serve.choose("best", "coop", 23.0, have)[0].name == "ui-venus-2-9b"  # capped by the co-op budget
    assert serve.choose("best", "coop", 12.0, have - {"ui-venus-2-9b"})[0].name == "qwen3-vl-8b"
    m, reason = serve.choose("best", "solo", 23.0, {"qwen3.8-27b", "ui-venus-2-9b"})
    assert m.name == "qwen3.8-27b"
    assert "holo4-27b (not downloaded)" in reason and "qwen3.8-27b-q5 (not downloaded)" in reason
    m, reason = serve.choose("best", "solo", 19.0, have)
    assert m.name == "holo4-27b" and "min(free VRAM 19.0 GiB, solo budget 20.0 GiB)" in reason
    with pytest.raises(serve.ServeError, match="no downloaded model fits"):
        serve.choose("best", "coop", 8.0, have)
    with pytest.raises(serve.ServeError, match="--force"):
        serve.choose("best", "solo", None, have)
    assert serve.choose("best", "solo", None, have, force=True)[0].name == "holo4-27b"


def test_download_fetches_and_verifies_sizes(tmp_path):
    m = MODELS["ui-venus-2-9b"]
    fetched = []

    def fetcher(model, f, dest):
        fetched.append(f.name)
        with (dest / f.name).open("wb") as out:
            out.truncate(f.size)

    (tmp_path / m.repo).mkdir(parents=True)
    lines = []
    assert serve.download(m, tmp_path, fetcher, disk_free=lambda p: 100 * GIB, out=lines.append) == tmp_path / m.repo
    assert fetched == [m.weights[0].name, m.mmproj.name]
    assert "9.75 GiB needed, 100.00 GiB free" in lines[0]
    assert serve.downloaded(m, tmp_path)
    fetched.clear()
    serve.download(m, tmp_path, fetcher, disk_free=lambda p: 0, out=lines.append)  # nothing left to fetch
    assert fetched == []


def test_download_size_mismatch_and_disk_refusal(tmp_path):
    m = MODELS["ui-venus-2-9b"]

    def short(model, f, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f.name).write_bytes(b"x" * 10)

    with pytest.raises(serve.ServeError, match=f"{m.mmproj.name}: expected {m.mmproj.size} bytes, have 10"):
        serve.download(m, tmp_path, short, disk_free=lambda p: 100 * GIB, out=lambda s: None)
    assert not serve.downloaded(m, tmp_path)
    called = []
    with pytest.raises(serve.ServeError, match="not enough disk"):
        serve.download(m, tmp_path, lambda *a: called.append(a), disk_free=lambda p: 5 * GIB, out=lambda s: None)
    assert called == []


def test_https_fetch_resumes(tmp_path):
    m = MODELS["ui-venus-2-9b"]
    f = type(m.mmproj)("mmproj.gguf", 10)
    (tmp_path / "mmproj.gguf.part").write_bytes(b"0123")
    seen = []

    class Resp:
        status = 206

        def __init__(self, data):
            self.data = [data, b""]

        def read(self, n):
            return self.data.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        seen.append((req.full_url, req.headers.get("Range")))
        return Resp(b"456789")

    serve.https_fetch(m, f, tmp_path, opener)
    assert seen == [(f"https://huggingface.co/{m.repo}/resolve/main/mmproj.gguf", "bytes=4-")]
    assert (tmp_path / "mmproj.gguf").read_bytes() == b"0123456789"
    assert not (tmp_path / "mmproj.gguf.part").exists()


def test_progress_counts_partial_files(tmp_path):
    m = MODELS["ui-venus-2-9b"]
    d = tmp_path / m.repo
    d.mkdir(parents=True)
    with (d / m.mmproj.name).open("wb") as out:
        out.truncate(m.mmproj.size)
    (d / (m.weights[0].name + ".part")).write_bytes(b"x" * 100)
    assert serve.progress(m, tmp_path) == {"bytes": m.mmproj.size + 100,
                                           "total": m.weights[0].size + m.mmproj.size}
    hf = d / ".cache" / "huggingface" / "download"
    hf.mkdir(parents=True)
    (hf / "Ab12.etag.incomplete").write_bytes(b"x" * 50)  # hf download --local-dir in flight
    assert serve.progress(m, tmp_path)["bytes"] == m.mmproj.size + 150
    vl = MODELS["qwen3-vl-8b"]
    assert not serve.downloaded(vl)
    fake_download(vl, None)  # the hub cache (HF_HUB_CACHE, see conftest)
    assert serve.downloaded(vl) and "qwen3-vl-8b" in serve.downloaded_names(tmp_path)


def test_gpu_reading(tmp_path):
    smi = tmp_path / "nvidia-smi"
    smi.write_text("#!/bin/sh\necho '3400, 20900, 24564'\n")
    smi.chmod(0o755)
    reading = serve.gpu(("/nonexistent/nvidia-smi", str(smi)))
    assert reading == {"used_mib": 3400, "free_mib": 20900, "total_mib": 24564}
    assert serve.free_gib(reading) == pytest.approx(20.41, abs=0.01)
    assert serve.gpu(("/nonexistent/nvidia-smi",)) is None


def test_status_parsing():
    llama = json.dumps({"object": "list", "data": [{"id": "holo4-27b", "object": "model", "owned_by": "llamacpp"}]})
    assert serve.parse_models(llama) == ["holo4-27b"]
    assert serve.parse_models("not json") == [] and serve.parse_models("[]") == []
    assert serve.registry_name(["Qwen/Qwen3-VL-8B-Instruct-FP8"]) == "qwen3-vl-8b"
    assert serve.registry_name(["/home/raph/models/Hcompany/Holo4-27B-GGUF/q4_k_m.gguf"]) == "holo4-27b"
    assert serve.registry_name(["something-else"]) is None and serve.registry_name(None) is None


def sleeper(argv, cwd, env, log):
    return subprocess.Popen(["sleep", "30"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def test_launch_wait_record_and_stop(tmp_path):
    procs = ProcessManager(tmp_path / "state", launcher=sleeper, find=lambda args: [])
    m = MODELS["ui-venus-2-9b"]
    pid = serve.launch(procs, m, "coop", LLAMA, Path("/mnt/d/models"), help_text=HELP_NEW)
    record = procs.managed("model")
    assert record["pid"] == pid and record["model"] == m.name and record["mode"] == "coop" and record["drvfs"]
    assert record["argv"][-2:] == ["--load-mode", "none"]
    assert (tmp_path / "state" / "logs" / "model.log").exists()

    answers = iter([False, False, True])
    assert serve.wait_ready(procs, "llama", probe=lambda s, p: next(answers), poll=0) == "ready"
    t = iter(range(100))
    assert serve.wait_ready(procs, "llama", timeout=3, probe=lambda s, p: False, poll=0,
                            clock=lambda: next(t), sleep=lambda s: None) == "timeout"

    entry = serve.record_load(procs, 42.04)
    assert entry["seconds"] == 42.0 and entry["load"] == "no-mmap" and entry["drvfs"] is True
    assert serve.record_load(procs, 50) is None  # once per server
    assert procs.managed("model")["ready_s"] == 42.0
    loads = (tmp_path / "state" / "logs" / "model_loads.jsonl").read_text().splitlines()
    assert len(loads) == 1 and json.loads(loads[0])["model"] == m.name
    assert "ready after 42.0 s" in (tmp_path / "state" / "logs" / "model.log").read_text()

    info = serve.status_info(procs, reading={"used_mib": 1, "free_mib": 2, "total_mib": 3}, ids=["ui-venus-2-9b"])
    assert info["running"] and info["pids"] == [pid] and info["model"] == m.name and info["mode"] == "coop"
    assert serve.stop_server(procs, grace=2, poll=0.05) == [pid]
    assert procs.managed("model") is None
    assert serve.wait_ready(procs, "llama", probe=lambda s, p: False, poll=0) == "exited"


def test_find_llama_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "path"))
    exe = tmp_path / "llama.cpp" / "build" / "bin" / "llama-server"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    other = tmp_path / "other-llama-server"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    assert serve.find_llama_server() == str(exe)
    monkeypatch.setenv("LLAMA_SERVER", str(other))
    assert serve.find_llama_server() == str(other)
    assert serve.find_llama_server(str(exe)) == str(exe)
    assert serve.find_llama_server(str(tmp_path / "missing")) is None


def run_cli(*args, env=None):
    return subprocess.run([sys.executable, "-m", "sts_cua.serve", *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=60)


def test_cli_start_refuses_without_vram_and_list_runs(tmp_path):
    common = ["--models-dir", str(tmp_path / "models"), "--state-dir", str(tmp_path / "state")]
    env = {"PATH": "/usr/bin:/bin"}  # no nvidia-smi
    out = run_cli(*common, "list", env=env)
    assert out.returncode == 0 and "holo4-27b" in out.stdout and f"models dir {tmp_path / 'models'}" in out.stdout
    out = run_cli(*common, "start", "best", "--mode", "coop", "--port", "1", env=env)
    assert out.returncode == 2 and "--force" in out.stderr
    out = run_cli(*common, "start", "holo4-27b", "--mode", "solo", "--port", "1", env=env)
    assert out.returncode == 2 and "isn't downloaded" in out.stderr
    assert run_cli(*common, "start", "gpt-6", "--mode", "solo").returncode == 2  # argparse choices
    out = run_cli(*common, "status", "--json", "--port", "1", env=env)
    assert out.returncode == 0 and json.loads(out.stdout)["running"] is False


def test_cli_start_waits_for_a_fake_llama_server(tmp_path):
    """A fake llama-server that answers /health after a moment; start returns once it's ready, stop kills it."""
    m = MODELS["ui-venus-2-9b"]
    fake_download(m, tmp_path / "models")
    exe = tmp_path / "llama-server"
    exe.write_text(f"""#!{sys.executable}
import http.server, sys, time
port = int(sys.argv[sys.argv.index("--port") + 1])
time.sleep(0.5)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{{"status": "ok"}}' if self.path == "/health" else b'{{"data": [{{"id": "ui-venus-2-9b"}}]}}'
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
print("fake llama-server", " ".join(sys.argv[1:]), flush=True)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
""")
    exe.chmod(0o755)
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    common = ["--models-dir", str(tmp_path / "models"), "--state-dir", str(tmp_path / "state")]
    try:
        out = run_cli(*common, "start", m.name, "--mode", "coop", "--force", "--port", str(port),
                      "--llama-server", str(exe), "--timeout", "20", env={"PATH": "/usr/bin:/bin"})
        assert out.returncode == 0, out.stderr
        assert "ready after" in out.stdout and "forced past" in out.stdout
        status = json.loads(run_cli(*common, "status", "--json", "--port", str(port)).stdout)
        assert status["running"] and status["model"] == m.name and status["served"] == ["ui-venus-2-9b"]
        log = (tmp_path / "state" / "logs" / "model.log").read_text()
        assert "fake llama-server -m" in log and f"--alias {m.served_name}" in log
        out = run_cli(*common, "start", m.name, "--mode", "coop", "--force", "--port", str(port),
                      "--llama-server", str(exe))
        assert out.returncode == 2 and "already running" in out.stderr
    finally:
        out = run_cli(*common, "stop")
    assert out.returncode == 0 and "stopped" in out.stdout
    deadline = time.monotonic() + 5
    while serve.port_open(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not serve.port_open(port)


def test_budgets_match_registry():
    assert budget("coop") == 13.0 and budget("solo") == 20.0
