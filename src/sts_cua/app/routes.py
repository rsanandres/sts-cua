"""The control app's API: fixed actions and read-only views of runs, logs and memory (docs/app-api.md).

Nothing here runs a command string from a request. Every child process argv is built
from validated fields: model names from an allowlist, numbers clamped, names matched
with files.NAME_RE and paths checked to stay inside the repo's runs/ directory.
"""

from __future__ import annotations

import glob
import io
import json
import math
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

from ..models.factory import LOCAL_MODELS
from ..state import DEFAULT_URL, fetch_state, summarize
from ..supervisor import BUDGET_TZ, spent_today
from . import files
from .procs import RUN_PATTERN, ProcessManager

CLAUDE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
RUN_MODELS = (*CLAUDE_MODELS, *LOCAL_MODELS, "scripted")
MAX_DAILY_BUDGET = 5.0
DEFAULT_DAILY_BUDGET = 2.0
MAX_RUNS = 1000
MEMORY_STORE = "memory/notes.jsonl"
NOTE_STATUSES = ("all", "probation", "active", "retired")
# The docs/hosting.md vLLM command, minus the model.
VLLM_FLAGS = ["--gpu-memory-utilization", "0.55", "--max-model-len", "8192", "--max-num-seqs", "1",
              "--limit-mm-per-prompt", '{"image":1,"video":0}']
VLLM_READY = "Application startup complete"  # uvicorn's line once vLLM's server is up
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
    nvidia_smi: tuple[str, ...] = NVIDIA_SMI
    shutdown_delay: float = 1.5  # seconds between replying and running wsl --shutdown

    @property
    def runs(self) -> Path:
        return self.repo / "runs"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Reply:
    """A non-JSON response (images)."""

    body: bytes
    content_type: str
    status: int = 200


def capture_png(display: str) -> bytes:
    from ..screen import X11Screen

    image = X11Screen(display).capture()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# ---- validation

def _choice(body: dict, key: str, options, default=None):
    value = body.get(key, default)
    if value not in options:
        raise ApiError(400, f"{key} must be one of {', '.join(map(str, options))}")
    return value


def _number(body: dict, key: str, default, lo: float, hi: float, integer: bool = False):
    """A JSON number clamped to [lo, hi]; null or missing gives default."""
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError(400, f"{key} must be a number")
    if integer and value != int(value):
        raise ApiError(400, f"{key} must be an integer")
    value = min(max(value, lo), hi)
    return int(value) if integer else float(value)


def _query_int(query: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = query.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, f"{key} must be an integer") from None
    return min(max(value, lo), hi)


def _name(value, what: str) -> str:
    if not isinstance(value, str) or not files.safe_name(value):
        raise ApiError(400, f"{what} must match {files.NAME_RE.pattern}")
    return value


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

    argv = [python, "-m", "sts_cua.supervisor", "--display", display, "--model", model,
            "--daily-budget", f"{budget:g}"]
    if model in CLAUDE_MODELS:
        argv += ["--thinking", thinking, "--effort", effort]
    if max_runs is not None:
        argv += ["--max-runs", str(max_runs)]
    if build is not None:
        argv += ["--game-build", build]
    if memory:
        argv += ["--memory", MEMORY_STORE]
    settings = {"model": model, "thinking": thinking, "effort": effort, "daily_budget": budget,
                "max_runs": max_runs, "memory": memory, "game_build": build}
    return argv, settings


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
class App:
    config: AppConfig
    procs: ProcessManager
    capture: Callable[[str], bytes] = capture_png
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    costs: files.CostTracker = field(default_factory=files.CostTracker)
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
        for name in ("supervisor", "vllm"):
            if name in p.stopping:
                return f"{name}_stop"
        if p.managed("vllm") and VLLM_READY not in p.log_since_start("vllm", limit=4 << 20):
            return "vllm_start"
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
        state = summarize(fetch_state(self.config.state_url, timeout=1.0))
        if state is None:
            return {"reachable": False, "state_type": None, "act": None, "floor": None, "hp": None, "max_hp": None}
        player = state.get("player") or {}
        return {"reachable": True, "state_type": state.get("state_type"), "act": state.get("act"),
                "floor": state.get("floor"), "hp": player.get("hp"), "max_hp": player.get("max_hp")}

    def status(self, **_) -> dict:
        return {
            "processes": {name: self.procs.info(name) for name in ("steam", "xvfb", "game", "vllm", "supervisor")},
            "gpu": gpu_memory(self.config.nvidia_smi),
            "ram": ram(),
            "budget": self._budget(),
            "mod": self._mod(),
            "busy": self.busy(),
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

    def vllm_start(self, body: dict, **_) -> dict:
        model = _choice(body, "model", tuple(LOCAL_MODELS))
        if self.procs.info("vllm")["running"]:
            raise ApiError(409, "a model server is already running")
        exe = self.config.vllm
        if not exe:
            venv = self.config.repo / ".venv" / "bin" / "vllm"
            exe = str(venv) if venv.exists() else shutil.which("vllm")
        if not exe:
            raise ApiError(500, "vllm not found: install it on PATH or start the app with --vllm")
        pid = self.procs.start("vllm", [exe, "serve", LOCAL_MODELS[model], *VLLM_FLAGS], cwd=self.config.repo)
        return {"started": True, "pid": pid}

    def vllm_stop(self, **_) -> dict:
        return {"stopped": True, "pids": self.procs.stop("vllm")}

    def logs(self, name: str, query: dict, **_) -> dict:
        paths = {
            "steam": Path("/tmp/steam.log"),  # tools/stack_up.sh
            "game": Path("/tmp/sts2.log"),  # game.GameConfig.log_path
        }
        if name not in ("steam", "game", "vllm", "supervisor", "app", "stack", "reflect"):
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
            pid = self.procs.start("supervisor", argv, cwd=self.config.repo, env={"PYTHONUNBUFFERED": "1"},
                                   extra={"settings": settings})
            self._settings_path().write_text(json.dumps(settings) + "\n", encoding="utf-8")
        return {"started": True, "pid": pid}

    def runs_stop(self, **_) -> dict:
        return {"stopped": True, "pids": self.procs.stop("supervisor")}

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
                "outcome_source": score.get("outcome_source"),
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


_SEG = r"(?P<{}>[\w.-]+)"
# (method, path regex, handler name). Path groups become handler keyword arguments.
ROUTES = [
    ("GET", r"/api/health", "health"),
    ("GET", r"/api/status", "status"),
    ("POST", r"/api/stack/up", "stack_up"),
    ("POST", r"/api/stack/down", "stack_down"),
    ("POST", r"/api/wsl/shutdown", "wsl_shutdown"),
    ("POST", r"/api/vllm/start", "vllm_start"),
    ("POST", r"/api/vllm/stop", "vllm_stop"),
    ("GET", r"/api/logs/" + _SEG.format("name"), "logs"),
    ("POST", r"/api/runs/start", "runs_start"),
    ("POST", r"/api/runs/stop", "runs_stop"),
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
