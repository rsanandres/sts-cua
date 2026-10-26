"""The control app's API: fixed actions and read-only views of runs, logs and memory (docs/app-api.md).

Nothing here runs a command string from a request. Every child process argv is built
from validated fields: model names from an allowlist, numbers clamped, names matched
with files.NAME_RE and paths checked to stay inside the repo's runs/ directory.
"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .. import coop, serve
from ..models.factory import LOCAL_MODELS
from ..models.registry import MODELS, budget
from ..state import DEFAULT_URL, fetch_state, summarize
from ..score import run_mode
from ..supervisor import BUDGET_TZ, spent_today
from . import files
from .bench import BENCH, INSTALL, BenchRoutes, stop_jobs
from .procs import RUN_PATTERN, ProcessManager
from .validate import ApiError, Reply, _choice, _flag, _name, _number, _query_int  # noqa: F401 (re-exported)

CLAUDE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
RUN_MODELS = (*CLAUDE_MODELS, *LOCAL_MODELS, "scripted")
MAX_DAILY_BUDGET = 5.0
DEFAULT_DAILY_BUDGET = 2.0
MAX_RUNS = 1000
MAX_COOP_COST = 5.0
DEFAULT_COOP_COST = 1.0
COOP_IDLE_MINUTES = (5.0, 120.0)
MEMORY_STORE = "memory/notes.jsonl"
NOTE_STATUSES = ("all", "probation", "active", "retired")
MODEL_CHOICES = (serve.BEST, *MODELS)  # POST /api/model/start: the registry, or the best that fits
STACK_READY = "stack is up"  # tools/stack_up.sh
NVIDIA_SMI = ("/usr/lib/wsl/lib/nvidia-smi", "nvidia-smi")
REFLECT_PATTERN = r"python[0-9.]* -m sts_cua\.memory reflect"


def version() -> str:
    try:
        return metadata.version("sts-cua")
    except metadata.PackageNotFoundError:
        return "0.1.0"


@dataclass
class AppConfig:
    repo: Path
    state_dir: Path  # pid files in run/, logs in logs/
    token_file: Path
    host: str = "127.0.0.1"
    port: int = 8765
    display: str = ":99"
    state_url: str = DEFAULT_URL
    python: str | None = None  # None: <repo>/.venv/bin/python if present, else this interpreter
    vllm: str | None = None  # None: <repo>/.venv/bin/vllm if present, else vllm on PATH
    llama_server: str | None = None  # None: $LLAMA_SERVER, ~/llama.cpp/build/bin/llama-server, PATH
    models_dir: Path | None = None  # None: serve.models_root ($STS_CUA_MODELS, ~/models or /mnt/d/models)
    model_port: int = serve.PORT
    nvidia_smi: tuple[str, ...] = NVIDIA_SMI
    shutdown_delay: float = 1.5  # seconds between replying and running wsl --shutdown

    @property
    def runs(self) -> Path:
        return self.repo / "runs"


def capture_png(display: str) -> bytes:
    from ..screen import X11Screen

    image = X11Screen(display).capture()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# ---- argv builders

LOCAL_THINKING = ("on", "off")  # --local-thinking
LOCAL_EFFORTS = ("low", "medium", "xhigh")  # --local-effort (models/factory.py)


def local_flags(body: dict, model: str, coop: bool) -> tuple[list[str], dict]:
    """--local-thinking / --local-effort for a registry model, from "local_thinking" and "local_effort".

    Both null (or missing) leave the factory's defaults: thinking off in co-op, else the registry's.
    Effort needs thinking on, and neither applies to a model without a thinking mode or to Claude.
    """
    thinking = _choice(body, "local_thinking", (None, *LOCAL_THINKING))
    effort = _choice(body, "local_effort", (None, *LOCAL_EFFORTS))
    m = MODELS.get(model)
    if m is None:
        if thinking is not None or effort is not None:
            raise ApiError(400, "local_thinking and local_effort are for local models only")
        return [], {}
    if not m.thinking and (thinking == "on" or effort is not None):
        raise ApiError(400, f"{model} has no thinking mode")
    thinking_on = thinking == "on" if thinking is not None else (False if coop else m.thinking)
    if effort is not None and not thinking_on:
        raise ApiError(400, "local_effort applies only with thinking on" +
                       (" (co-op defaults to off)" if coop and thinking is None else ""))
    argv = (["--local-thinking", thinking] if thinking else []) + (["--local-effort", effort] if effort else [])
    return argv, {"local_thinking": thinking, "local_effort": effort}


def supervisor_argv(body: dict, python: str, display: str) -> tuple[list[str], dict]:
    """The `python -m sts_cua.supervisor` argv for a /api/runs/start body, and the validated settings."""
    model = _choice(body, "model", RUN_MODELS)
    thinking = _choice(body, "thinking", ("off", "on"), "off")
    effort = _choice(body, "effort", ("low", "medium", "high"), "low")
    budget = _number(body, "daily_budget", DEFAULT_DAILY_BUDGET, 0.0, MAX_DAILY_BUDGET)
    max_runs = _number(body, "max_runs", None, 1, MAX_RUNS, integer=True)
    memory = body.get("memory", False)
    if not isinstance(memory, bool):
        raise ApiError(400, "memory must be true or false")
    build = body.get("game_build")
    if build is not None:
        build = _name(build, "game_build")

    local, local_settings = local_flags(body, model, coop=False)

    argv = [python, "-m", "sts_cua.supervisor", "--display", display, "--model", model,
            "--daily-budget", f"{budget:g}"]
    if model in CLAUDE_MODELS:
        argv += ["--thinking", thinking, "--effort", effort]
    argv += local
    if max_runs is not None:
        argv += ["--max-runs", str(max_runs)]
    if build is not None:
        argv += ["--game-build", build]
    if memory:
        argv += ["--memory", MEMORY_STORE]
    settings = {"model": model, "thinking": thinking, "effort": effort, "daily_budget": budget,
                "max_runs": max_runs, "memory": memory, "game_build": build, **local_settings}
    return argv, settings


def coop_argv(body: dict, python: str, display: str) -> tuple[list[str], dict]:
    """The `python -m sts_cua --coop` argv for a /api/coop/start body, and the validated settings.

    Defaults are the fastest settings (coop.py): Haiku 5.5, thinking off, rules on.
    """
    model = _choice(body, "model", RUN_MODELS, "claude-haiku-5-5")
    thinking = _choice(body, "thinking", ("off", "on"), "off")
    effort = _choice(body, "effort", ("low", "medium", "high"), "low")
    max_cost = _number(body, "max_cost", DEFAULT_COOP_COST, 0.0, MAX_COOP_COST)
    idle = _number(body, "idle_minutes", coop.IDLE_MINUTES, *COOP_IDLE_MINUTES)
    rules = body.get("rules", True)
    if not isinstance(rules, bool):
        raise ApiError(400, "rules must be true or false")
    build = body.get("game_build")
    if build is not None:
        build = _name(build, "game_build")

    local, local_settings = local_flags(body, model, coop=True)

    argv = [python, "-m", "sts_cua", "--coop", "--display", display, "--model", model,
            "--max-cost", f"{max_cost:g}", "--coop-idle-minutes", f"{idle:g}"]
    if model in CLAUDE_MODELS:
        argv += ["--thinking", thinking, "--effort", effort]
    argv += local
    if build is not None:
        argv += ["--game-build", build]
    if not rules:
        argv += ["--no-rules"]
    settings = {"model": model, "thinking": thinking, "effort": effort, "max_cost": max_cost,
                "idle_minutes": idle, "rules": rules, "game_build": build, **local_settings}
    return argv, settings


def _coop_mark(meta: dict) -> dict:
    """{"mode": "coop"} for a co-op run (score.run_mode), nothing for solo runs."""
    return {"mode": "coop"} if run_mode(meta) == "coop" else {}


# ---- system readings

def gpu_memory(commands=NVIDIA_SMI) -> dict | None:
    for exe in commands:
        try:
            out = subprocess.run([exe, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5)
            used, total = (int(float(x)) for x in out.stdout.splitlines()[0].split(","))
        except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
            continue
        if out.returncode == 0:
            return {"used_mib": used, "total_mib": total}
    return None


def ram(meminfo: Path = Path("/proc/meminfo")) -> dict | None:
    try:
        fields = {line.split(":")[0]: int(line.split()[1]) for line in meminfo.read_text().splitlines()
                  if line.endswith("kB")}
        total, available = fields["MemTotal"], fields["MemAvailable"]
    except (OSError, KeyError, ValueError, IndexError):
        return None
    return {"used_mib": (total - available) // 1024, "total_mib": total // 1024}


def interop_env() -> dict[str, str]:
    """WSL_INTEROP pointing at a live socket. A server left running after the wsl.exe session
    that started it has ended may hold a stale one (assumption, not verified on the PC)."""
    current = os.environ.get("WSL_INTEROP")
    if current and os.path.exists(current):
        return {}
    sockets = sorted(glob.glob("/run/WSL/*_interop"), key=lambda p: os.path.getmtime(p), reverse=True)
    return {"WSL_INTEROP": sockets[0]} if sockets else {}


# ---- the API

@dataclass
class App(BenchRoutes):
    config: AppConfig
    procs: ProcessManager
    capture: Callable[[str], bytes] = capture_png
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    costs: files.CostTracker = field(default_factory=files.CostTracker)
    vram: Callable[[], dict | None] | None = None  # None: serve.gpu(config.nvidia_smi)
    probe: Callable[[str, int], bool] = serve.ready  # the model server answers its readiness URL
    served: Callable[[int], list[str] | None] = serve.served  # ids /v1/models lists
    shutting_down: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _screen_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def python(self) -> str:
        if self.config.python:
            return self.config.python
        venv = self.config.repo / ".venv" / "bin" / "python"
        return str(venv) if venv.exists() else sys.executable

    def _settings_path(self) -> Path:
        return self.config.state_dir / "last_run.json"

    def _run_dir(self, run_id: str) -> Path:
        path = files.inside(self.config.runs, _name(run_id, "run_id"))
        if path is None or not (path / "meta.json").exists():
            raise ApiError(404, f"no run {run_id}")
        return path

    # status

    def health(self, **_) -> dict:
        return {"ok": True, "version": version()}

    def busy(self) -> str | None:
        p = self.procs
        if self.shutting_down:
            return "wsl_shutdown"
        if p.managed("stack_down"):
            return "stack_down"
        if p.managed("stack_up") and STACK_READY not in p.log_since_start("stack_up"):
            return "stack_up"
        for name in ("supervisor", serve.NAME, "coop"):
            if name in p.stopping:
                return f"{name}_stop"
        model = p.managed(serve.NAME)
        if model and "ready_s" not in model and not self.probe(model.get("server", "llama"), model.get("port", self.config.model_port)):
            return "model_start"
        for name in (BENCH, INSTALL):  # long jobs: the page keeps Stack down usable during them
            if p.managed(name):
                return name
        if p.managed("reflect"):
            return "reflect"
        return None

    def _budget(self) -> dict:
        now = self.clock()
        last = files.read_json(self._settings_path()) or {}
        return {
            "date": now.astimezone(ZoneInfo(BUDGET_TZ)).date().isoformat(),
            "spent_usd": round(spent_today(self.config.runs, now, BUDGET_TZ), 6),
            "cap_usd": float(last.get("daily_budget", DEFAULT_DAILY_BUDGET)),
        }

    def _mod(self) -> dict:
        # The singleplayer endpoint answers 409 during a co-op run; coop.fetch_state falls back.
        state = summarize(fetch_state(self.config.state_url, timeout=1.0)
                          or fetch_state(coop.multiplayer_url(self.config.state_url), timeout=1.0))
        if state is None:
            return {"reachable": False, "state_type": None, "act": None, "floor": None, "hp": None, "max_hp": None}
        player = state.get("player") or {}
        return {"reachable": True, "state_type": state.get("state_type"), "act": state.get("act"),
                "floor": state.get("floor"), "hp": player.get("hp"), "max_hp": player.get("max_hp")}

    def status(self, **_) -> dict:
        return {
            "processes": {name: self.procs.info(name) for name in ("steam", "xvfb", "game", serve.NAME, "supervisor")},
            "gpu": gpu_memory(self.config.nvidia_smi),
            "ram": ram(),
            "budget": self._budget(),
            "mod": self._mod(),
            "busy": self.busy(),
            "coop": self.procs.info("coop"),
            "bench": self.procs.info(BENCH),
            "install": self.procs.info(INSTALL),
        }

    # stack control

    def stack_up(self, **_) -> dict:
        if self.procs.managed("stack_down"):
            raise ApiError(409, "the stack is being stopped")
        if self.procs.managed("stack_up"):
            raise ApiError(409, "stack_up.sh is already running")
        self.procs.start("stack_up", ["bash", str(self.config.repo / "tools" / "stack_up.sh")],
                         cwd=self.config.repo, log_name="stack")
        return {"started": True}

    def stack_down(self, **_) -> dict:
        if self.procs.managed("stack_down"):
            raise ApiError(409, "the stack is already being stopped")
        # stack_up.sh waits in a loop and would run stack_down.sh again from its trap.
        self.procs.send("stack_up", signal.SIGKILL)
        stop_jobs(self.procs)  # a bench job or llama.cpp install the app started (stack_down.sh gets the rest)
        self.procs.start("stack_down", ["bash", str(self.config.repo / "tools" / "stack_down.sh")],
                         cwd=self.config.repo, log_name="stack")
        return {"started": True}

    def wsl_shutdown(self, body: dict, **_) -> dict:
        if body.get("confirm") is not True:
            raise ApiError(400, 'send {"confirm": true} to shut WSL down')
        self.shutting_down = True
        cmd = shutil.which("cmd.exe") or "/mnt/c/Windows/System32/cmd.exe"
        cwd = Path("/mnt/c") if Path("/mnt/c").is_dir() else self.config.repo

        def later():
            time.sleep(self.config.shutdown_delay)
            self.procs.start("wsl_shutdown", [cmd, "/c", "wsl", "--shutdown"], cwd=cwd, env=interop_env(),
                             log_name="app")

        threading.Thread(target=later, daemon=True, name="wsl-shutdown").start()
        return {"shutting_down": True}

    # model server (serve.py; one at a time on config.model_port)

    def _gpu_reading(self) -> dict | None:
        return self.vram() if self.vram else serve.gpu(self.config.nvidia_smi)

    def _models_root(self) -> Path:
        return serve.models_root(self.config.models_dir)

    def model_start(self, body: dict, **_) -> dict:
        name = _choice(body, "model", MODEL_CHOICES)
        mode = _choice(body, "mode", serve.MODES)
        force = body.get("force", False)
        if not isinstance(force, bool):
            raise ApiError(400, "force must be true or false")
        with self._lock:
            if self.procs.info(serve.NAME)["running"]:
                raise ApiError(409, "a model server is already running; stop it first")
            if self.procs.info(INSTALL)["running"]:
                raise ApiError(409, "llama.cpp is being installed; wait for it to finish")
            if self._bench_serving():
                raise ApiError(409, "a bench job is starting model servers itself; stop it or wait")
            root = self._models_root()
            try:
                m, reason = serve.choose(name, mode, serve.free_gib(self._gpu_reading()),
                                         serve.downloaded_names(root), force)
            except serve.ServeError as e:
                raise ApiError(409, str(e)) from None
            if m.server == "llama":
                exe = serve.find_llama_server(self.config.llama_server)
                if not exe:
                    raise ApiError(500, "llama-server not found: run tools/install_llama_cpp.sh or start the app "
                                        "with --llama-server")
            else:
                exe = serve.find_vllm(self.config.vllm, self.config.repo)
                if not exe:
                    raise ApiError(500, "vllm not found: install it on PATH or start the app with --vllm")
            pid = serve.launch(self.procs, m, mode, exe, root, self.config.model_port, reason)
        # Record the load time (logs/model_loads.jsonl) when it answers, even if no page is polling.
        threading.Thread(target=serve.watch_load, args=(self.procs, m.server, self.config.model_port),
                         kwargs={"probe": self.probe}, daemon=True, name="model-load").start()
        return {"started": True, "pid": pid, "model": m.name, "mode": mode, "reason": reason}

    def model_stop(self, **_) -> dict:
        return {"stopped": True, "pids": self.procs.stop(serve.NAME)}

    def vllm_start(self, body: dict, **_) -> dict:
        """The old endpoint: {"model"} only, mode solo."""
        return self.model_start({**body, "mode": body.get("mode", "solo")})

    def model_download(self, body: dict, **_) -> dict:
        name = _choice(body, "model", tuple(MODELS))
        with self._lock:
            running = self.procs.managed("download")
            if running:
                raise ApiError(409, f"already downloading {running.get('model')}")
            argv = [self.python, "-m", "sts_cua.serve", "--models-dir", str(self._models_root()), "download", name]
            pid = self.procs.start("download", argv, cwd=self.config.repo, env={"PYTHONUNBUFFERED": "1"},
                                   extra={"model": name})
        return {"started": True, "pid": pid, "model": name}

    def models(self, **_) -> dict:
        root = self._models_root()
        reading = self._gpu_reading()
        free = serve.free_gib(reading)
        record = self.procs.managed(serve.NAME) or {}
        running = self.procs.info(serve.NAME)
        ids = self.served(self.config.model_port) if running["running"] else None
        current = record.get("model") or serve.registry_name(ids)
        dl = self.procs.managed("download") or {}
        have = serve.downloaded_names(root)
        rows = []
        for m in sorted(MODELS.values(), key=lambda m: -m.rank):
            rows.append({
                "name": m.name, "family": m.family, "server": m.server, "repo": m.repo, "rank": m.rank,
                "file_gib": round(m.file_gib, 2), "need_gib": round(m.need_gib, 2),
                "fits": {mode: m.fits(budget(mode)) for mode in serve.MODES},
                "downloaded": m.name in have,
                "download": {**serve.progress(m, root), "running": dl.get("model") == m.name},
                "license": m.license, "thinking": m.thinking, "notes": m.notes,
                "running": running["running"] and current == m.name,
            })
        best = {}
        for mode in serve.MODES:
            limit = budget(mode) if free is None else min(free, budget(mode))
            pick = serve.best(limit, have)
            best[mode] = pick.name if pick else None
        return {
            "models": rows, "best": best, "gpu": reading,
            "server": {"running": running["running"], "pid": running["pid"], "model": current,
                       "mode": record.get("mode"), "served": ids, "port": record.get("port", self.config.model_port),
                       "ready": bool(running["running"] and ids)},
            "download": {"running": bool(dl), "model": dl.get("model")},
        }

    def logs(self, name: str, query: dict, **_) -> dict:
        paths = {
            "steam": Path("/tmp/steam.log"),  # tools/stack_up.sh
            "game": Path("/tmp/sts2.log"),  # game.GameConfig.log_path
        }
        if name not in ("steam", "game", "model", "download", "vllm", "supervisor", "app", "stack", "reflect", "coop",
                        BENCH, INSTALL):
            raise ApiError(404, f"no log {name}")
        path = paths.get(name) or self.procs.log_path(name)
        lines = _query_int(query, "lines", 200, 1, 2000)
        return {"name": name, "lines": files.tail_lines(path, lines)}

    # agent runs

    def runs_start(self, body: dict, **_) -> dict:
        argv, settings = supervisor_argv(body, self.python, self.config.display)
        with self._lock:
            if self.procs.info("supervisor")["running"]:
                raise ApiError(409, "a supervisor is already running")
            if self.procs.info("coop")["running"]:
                raise ApiError(409, "a co-op session is running on the agent's display")
            pid = self.procs.start("supervisor", argv, cwd=self.config.repo, env={"PYTHONUNBUFFERED": "1"},
                                   extra={"settings": settings})
            self._settings_path().write_text(json.dumps(settings) + "\n", encoding="utf-8")
        return {"started": True, "pid": pid}

    def runs_stop(self, **_) -> dict:
        return {"stopped": True, "pids": self.procs.stop("supervisor")}

    def coop_start(self, body: dict, **_) -> dict:
        """One co-op agent session (never the supervisor): python -m sts_cua --coop, log `coop`."""
        argv, settings = coop_argv(body, self.python, self.config.display)
        with self._lock:
            if self.procs.info("coop")["running"]:
                raise ApiError(409, "a co-op session is already running")
            if self.procs.info("supervisor")["running"]:
                raise ApiError(409, "a supervisor is running on the agent's display; stop it first")
            pid = self.procs.start("coop", argv, cwd=self.config.repo, env={"PYTHONUNBUFFERED": "1"},
                                   extra={"settings": settings})
        return {"started": True, "pid": pid, "settings": settings}

    def coop_stop(self, **_) -> dict:
        return {"stopped": True, "pids": self.procs.stop("coop")}

    def live(self, **_) -> dict:
        dirs = files.run_dirs(self.config.runs)
        if not dirs:
            return {"run_id": None, "kind": None, "model": None, "running": False, "step": 0, "cost_usd": 0.0,
                    "last": None, "screenshot": None}
        run = dirs[0]
        meta = files.read_json(run / "meta.json") or {}
        summary = files.read_json(run / "summary.json")
        steps = run / "steps.jsonl"
        last = files.last_record(steps)
        if summary is not None:
            step, cost = int(summary.get("steps") or 0), float(summary.get("cost_usd") or 0.0)
        else:
            step, cost = (int(last.get("step") or 0) if last else 0), self.costs.cost(steps)
        screenshot = None
        if last and isinstance(last.get("screenshot"), str):
            shot = Path(last["screenshot"]).name
            if files.safe_name(shot) and (run / "screenshots" / shot).is_file():
                screenshot = f"/api/runs/{run.name}/screenshots/{shot}"
        return {
            "run_id": run.name, "kind": meta.get("kind"), "model": meta.get("model"),
            **_coop_mark(meta),
            "running": summary is None and bool(self.procs.matching(RUN_PATTERN)),
            "step": step, "cost_usd": round(cost, 6),
            "last": None if last is None else {
                "step": last.get("step"), "thought": last.get("thought"), "executed": last.get("executed") or [],
                "error": last.get("error"), "state": last.get("state"), "notes_shown": last.get("notes_shown"),
                "time": last.get("time"),
            },
            "screenshot": screenshot,
        }

    def screen(self, **_) -> Reply:
        try:
            with self._screen_lock:
                png = self.capture(self.config.display)
        except Exception as e:  # mss raises its own errors when the display is down
            raise ApiError(503, f"can't capture {self.config.display}: {e}") from None
        return Reply(png, "image/png")

    # results

    def ledger(self, query: dict, **_) -> dict:
        limit = _query_int(query, "limit", 100, 1, 1000)
        entries = []
        for line in reversed(files.tail_lines(self.config.runs / "ledger.jsonl", limit)):
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
        return {"entries": entries}

    def runs_list(self, query: dict, **_) -> dict:
        limit = _query_int(query, "limit", 50, 1, 500)
        out = []
        for run in files.run_dirs(self.config.runs)[:limit]:
            meta = files.read_json(run / "meta.json") or {}
            summary = files.read_json(run / "summary.json") or {}
            score = files.read_json(run / "score.json") or {}
            out.append({
                "run_id": run.name, "kind": meta.get("kind"), "model": meta.get("model"),
                "started_at": meta.get("started_at"), "end_reason": summary.get("end_reason"),
                "steps": summary.get("steps"), "cost_usd": summary.get("cost_usd"), "outcome": score.get("outcome"),
                "floor": score.get("max_floor"), "score": score.get("score"), "seed": score.get("seed"),
                "outcome_source": score.get("outcome_source"), **_coop_mark(meta),
            })
        return {"runs": out}

    def run_detail(self, run_id: str, **_) -> dict:
        run = self._run_dir(run_id)
        return {"meta": files.read_json(run / "meta.json") or {}, "summary": files.read_json(run / "summary.json"),
                "score": files.read_json(run / "score.json")}

    def run_steps(self, run_id: str, query: dict, **_) -> dict:
        run = self._run_dir(run_id)
        offset = _query_int(query, "offset", 0, 0, 10**9)
        limit = _query_int(query, "limit", 50, 1, 500)
        total, steps = files.read_steps(run / "steps.jsonl", offset, limit)
        return {"total": total, "steps": steps}

    def run_screenshot(self, run_id: str, file: str, **_) -> Reply:
        run = self._run_dir(run_id)
        if not file.endswith(".png"):
            raise ApiError(404, "screenshots are .png files")
        path = files.inside(run, "screenshots", _name(file, "file"))
        if path is None or not path.is_file():
            raise ApiError(404, f"no screenshot {file}")
        return Reply(path.read_bytes(), "image/png")

    # memory

    def _store_path(self) -> Path:
        return self.config.repo / MEMORY_STORE

    def _reflecting(self) -> bool:
        return bool(self.procs.managed("reflect") or self.procs.matching(REFLECT_PATTERN))

    def notes(self, query: dict, **_) -> dict:
        from ..memory import NoteStore

        status = _choice(query, "status", NOTE_STATUSES, "all")
        try:
            store = NoteStore.load(self._store_path())
        except (ValueError, TypeError, KeyError) as e:
            raise ApiError(500, f"can't read {self._store_path()}: {e}") from None
        notes = [n.to_dict() for n in store.notes if status == "all" or n.status == status]
        return {"path": str(self._store_path()), "notes": notes}

    def retire(self, note_id: str, **_) -> dict:
        from ..memory import NoteStore
        from ..memory.store import now

        _name(note_id, "note id")
        with self._lock:
            if self._reflecting():
                raise ApiError(409, "a reflect run is editing the store; try again when it finishes")
            store = NoteStore.load(self._store_path())
            note = store.get(note_id)
            if note is None:
                raise ApiError(404, f"no note {note_id}")
            note.status, note.updated = "retired", now()
            store.save()
        return {"retired": True}

    def reflect(self, body: dict, **_) -> dict:
        run_id = _name(body.get("run_id"), "run_id")
        self._run_dir(run_id)
        with self._lock:
            if self._reflecting():
                raise ApiError(409, "a reflect run is already going")
            self.procs.start("reflect", [self.python, "-m", "sts_cua.memory", "reflect", f"runs/{run_id}"],
                             cwd=self.config.repo, env={"PYTHONUNBUFFERED": "1"})
        return {"started": True}

    # memory A/B evaluations (sts_cua.eval), read-only

    def _eval(self, d: Path) -> tuple[dict, list[dict], dict]:
        from .. import eval as evaluation

        plan, results = evaluation.load_plan(d), evaluation.load_results(d)
        saved = files.read_json(d / evaluation.REPORT_FILE) or {}
        return plan, results, evaluation.build_report(plan, results, stop_reason=saved.get("stop_reason"))

    def evals_list(self, **_) -> dict:
        from .. import eval as evaluation

        out = []
        for d in evaluation.list_evals(self.config.runs):
            try:
                plan, _, report = self._eval(d)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue  # a broken plan is skipped, not a 500 for the whole list
            out.append({
                "name": d.name, "created_at": plan.get("created_at"), "mode": report["mode"], "arms": report["arms"],
                "planned_runs": report["planned_runs"], "valid_runs": report["valid_runs"],
                "complete": report["complete"], "stop_reason": report["stop_reason"], "cost_usd": report["cost_usd"],
                "p_value": (report["test"] or {}).get("p_value"),
            })
        out.sort(key=lambda e: e["created_at"] or "", reverse=True)
        return {"evals": out}

    def eval_detail(self, name: str, **_) -> dict:
        from .. import eval as evaluation

        d = files.inside(self.config.runs / evaluation.EVALS_DIR, _name(name, "name"))
        if d is None or not (d / evaluation.PLAN_FILE).is_file():
            raise ApiError(404, f"no evaluation {name}")
        try:
            plan, results, report = self._eval(d)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            raise ApiError(500, f"can't read evaluation {name}: {e}") from None
        return {"plan": plan, "report": report, "results": results}


_SEG = r"(?P<{}>[\w.-]+)"
# (method, path regex, handler name). Path groups become handler keyword arguments.
ROUTES = [
    ("GET", r"/api/health", "health"),
    ("GET", r"/api/status", "status"),
    ("POST", r"/api/stack/up", "stack_up"),
    ("POST", r"/api/stack/down", "stack_down"),
    ("POST", r"/api/wsl/shutdown", "wsl_shutdown"),
    ("GET", r"/api/models", "models"),
    ("POST", r"/api/model/start", "model_start"),
    ("POST", r"/api/model/stop", "model_stop"),
    ("POST", r"/api/model/download", "model_download"),
    ("POST", r"/api/vllm/start", "vllm_start"),  # aliases of /api/model/start (mode solo) and /api/model/stop
    ("POST", r"/api/vllm/stop", "model_stop"),
    ("GET", r"/api/logs/" + _SEG.format("name"), "logs"),
    ("POST", r"/api/runs/start", "runs_start"),
    ("POST", r"/api/runs/stop", "runs_stop"),
    ("POST", r"/api/coop/start", "coop_start"),
    ("POST", r"/api/coop/stop", "coop_stop"),
    ("GET", r"/api/live", "live"),
    ("GET", r"/api/screen\.png", "screen"),
    ("GET", r"/api/ledger", "ledger"),
    ("GET", r"/api/runs", "runs_list"),
    ("GET", r"/api/runs/" + _SEG.format("run_id"), "run_detail"),
    ("GET", r"/api/runs/" + _SEG.format("run_id") + "/steps", "run_steps"),
    ("GET", r"/api/runs/" + _SEG.format("run_id") + "/screenshots/" + _SEG.format("file"), "run_screenshot"),
    ("GET", r"/api/memory/notes", "notes"),
    ("POST", r"/api/memory/notes/" + _SEG.format("note_id") + "/retire", "retire"),
    ("POST", r"/api/memory/reflect", "reflect"),
    ("GET", r"/api/evals", "evals_list"),
    ("GET", r"/api/evals/" + _SEG.format("name"), "eval_detail"),
    ("GET", r"/api/llama", "llama"),
    ("POST", r"/api/llama/install", "llama_install"),
    ("GET", r"/api/bench", "bench"),
    ("GET", r"/api/bench/results", "bench_results"),
    ("GET", r"/api/bench/review/" + _SEG.format("file"), "bench_sheet"),
    ("POST", r"/api/bench/stop", "bench_stop"),
    ("POST", r"/api/bench/(?P<action>extract|review|delete|grounding|baseline|resources|clicks|write-docs)", "bench_start"),
]
COMPILED = [(method, re.compile(pattern + "$"), name) for method, pattern, name in ROUTES]
OPEN = {"health"}  # no token needed


def match(method: str, path: str) -> tuple[str, dict] | None:
    """(handler name, path arguments) for a request, ApiError 405 if only the method is wrong, None if no route."""
    wrong_method = False
    for m, pattern, name in COMPILED:
        found = pattern.match(path)
        if found:
            if m == method:
                return name, found.groupdict()
            wrong_method = True
    if wrong_method:
        raise ApiError(405, f"{method} not allowed on {path}")
    return None
