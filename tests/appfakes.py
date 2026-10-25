"""A control-app server on a free port over a fake repo, and helpers to call it with urllib."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from sts_cua.app.procs import ProcessManager
from sts_cua.app.routes import AppConfig
from sts_cua.app.server import make_server
from sts_cua.memory import NoteStore

NOW = datetime(2026, 10, 25, 19, 0, tzinfo=timezone.utc)  # Oct 25, 12:00 in Los Angeles
OLD_RUN = "20261025T100000Z"
LIVE_RUN = "20261025T120000Z"


def png_bytes(color=(10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def step_record(step: int, cost: float = 0.001, **extra) -> dict:
    return {
        "step": step, "time": f"2026-10-25T12:00:{step:02d}+00:00", "screenshot": f"screenshots/step_{step:04d}.png",
        "thought": f"thought {step}", "proposed": [], "executed": [{"type": "click", "x": step, "y": 1}],
        "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": cost},
        "state": {"state_type": "monster", "floor": 2}, "raw": {"big": "x" * 100}, **extra,
    }


def write_run(runs: Path, name: str, steps: int, finished: bool, kind: str = "game") -> Path:
    run = runs / name
    (run / "screenshots").mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"kind": kind, "model": "claude-haiku-5-5",
                                               "started_at": f"{name[:4]}-10-25T00:00:00+00:00"}))
    with (run / "steps.jsonl").open("w") as f:
        for i in range(1, steps + 1):
            f.write(json.dumps(step_record(i)) + "\n")
            (run / "screenshots" / f"step_{i:04d}.png").write_bytes(png_bytes())
    if finished:
        (run / "summary.json").write_text(json.dumps({
            "end_reason": "run_over:died", "steps": steps, "cost_usd": 0.25, "ended_at": "2026-10-25T18:00:00+00:00"}))
        (run / "score.json").write_text(json.dumps({"outcome": "died", "score": 7.0, "max_floor": 7}))
    return run


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    runs = repo / "runs"
    write_run(runs, OLD_RUN, 3, finished=True)
    write_run(runs, LIVE_RUN, 4, finished=False)
    (runs / "not-a-run").mkdir()
    with (runs / "ledger.jsonl").open("w") as f:
        for i in range(3):
            f.write(json.dumps({"run_dir": f"runs/r{i}", "score": float(i)}) + "\n")
    store = NoteStore(repo / "memory" / "notes.jsonl")
    store.add("enemy", "Block before the big hit.", {"enemy": "Jaw Worm"})
    store.add("ui", "End turn is bottom right.")
    store.notes[1].status = "active"
    store.save()
    (root / "outside.png").write_bytes(png_bytes())
    return repo


@dataclass
class FakeLauncher:
    """Records each argv and starts a harmless `sleep` in its place."""

    calls: list[dict] = field(default_factory=list)
    procs: list[subprocess.Popen] = field(default_factory=list)

    def __call__(self, argv, cwd, env, log):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "env": env})
        proc = subprocess.Popen(["sleep", "60"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(proc)
        return proc

    def argv(self, index: int = -1) -> list[str]:
        return self.calls[index]["argv"]

    def kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()


class Client:
    def __init__(self, base: str, token: str | None = None):
        self.base = base
        self.token = token
        no_redirect = type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                           {"redirect_request": lambda *a, **k: None})
        self.opener = urllib.request.build_opener(no_redirect)

    def request(self, path: str, method: str = "GET", body=None, headers: dict | None = None,
                token: str | None = "default", raw_body: bytes | None = None):
        """(status, parsed JSON or bytes, headers)."""
        h = dict(headers or {})
        token = self.token if token == "default" else token
        if token:
            h.setdefault("Authorization", f"Bearer {token}")
        data = raw_body
        if method == "POST" and data is None:
            data = json.dumps(body if body is not None else {}).encode()
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            resp = self.opener.open(req, timeout=10)
            status, payload, rh = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            status, payload, rh = e.code, e.read(), e.headers
        if (rh.get("Content-Type") or "").startswith("application/json"):
            payload = json.loads(payload)
        return status, payload, rh

    def get(self, path: str, **kw):
        status, payload, _ = self.request(path, **kw)
        return status, payload

    def post(self, path: str, body=None, **kw):
        status, payload, _ = self.request(path, method="POST", body=body, **kw)
        return status, payload


@dataclass
class Running:
    server: object
    client: Client
    launcher: FakeLauncher
    repo: Path
    state_dir: Path
    token: str

    @property
    def app(self):
        return self.server.app


def start_server(tmp_path: Path, capture=None) -> Running:
    repo = make_repo(tmp_path)
    state_dir = tmp_path / "state"
    config = AppConfig(
        repo=repo, state_dir=state_dir, token_file=state_dir / "app-token", port=0,
        state_url="http://127.0.0.1:9/api/v1/singleplayer", python="/venv/bin/python",
        vllm="/opt/vllm/bin/vllm", nvidia_smi=("/nonexistent/nvidia-smi",), shutdown_delay=0.05,
    )
    launcher = FakeLauncher()
    procs = ProcessManager(state_dir, launcher=launcher)
    kwargs = {"clock": lambda: NOW}
    if capture is not None:
        kwargs["capture"] = capture
    server = make_server(config, procs, **kwargs)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    token = (state_dir / "app-token").read_text().strip()
    client = Client(f"http://127.0.0.1:{server.server_address[1]}", token)
    return Running(server, client, launcher, repo, state_dir, token)


def stop_server(running: Running) -> None:
    running.server.shutdown()
    running.server.server_close()
    running.launcher.kill_all()
