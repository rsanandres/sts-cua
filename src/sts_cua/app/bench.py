"""The control app's Bake-off tab and llama.cpp install (docs/app-api.md, "Bake-off" and "llama.cpp").

Each bake-off action is one `python -m sts_cua.bench ...` child named `bench` (pid file
run/bench.pid, log logs/bench.log), one at a time, with an argv built from validated
fields only: models from the registry plus the two Claude references, modes from
serve.MODES, clamped numbers, target ids matching TARGET_ID. The install is
`bash tools/install_llama_cpp.sh [--prebuilt [tag]]`, child `install`, log logs/install.log.

`bench grounding --serve` and `bench resources --serve` start (and grounding also stops)
model servers through `python -m sts_cua.serve`, the same pid file the Model server card
uses. So those jobs wait while the app is starting a model or installing llama.cpp, a
supervisor or co-op session on a local model is running, and the card's Start waits for
them.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import signal
import subprocess
from pathlib import Path

from .. import serve
from ..models.registry import MODELS, budget
from . import files
from .validate import ApiError, Reply, _choice, _flag, _name, _number, _query_int

BENCH = "bench"  # pid file run/bench.pid, log logs/bench.log
INSTALL = "install"  # pid file run/install.pid, log logs/install.log
REFERENCE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")  # grounding/clicks references; cost API credits
BENCH_MODELS = (*MODELS, *REFERENCE_MODELS)
BASELINE_LABELS = ("pre-server", "desktop")
CLICKS_DISPLAY = ":98"  # away from the game's :99
MAX_MODELS = 8
MAX_DELETE = 500
MAX_TARGET_ROWS = 2000
TARGET_ID = re.compile(r"^\w[\w.-]{0,127}$")  # no leading "-", which argparse would read as an option
SHEET = re.compile(r"^sheet-\d{1,4}\.png$")  # bench/review/ (targets.contact_sheets)
RELEASE_TAG = re.compile(r"^b\d{1,6}$")
DOCS = "docs/hosting.md"
OUTPUT_BYTES = 256 * 1024
INSTALL_TAIL = 12
_TOUCHED = re.compile(r"measure rows: (.*)$")  # bench report --write-docs

_versions: dict[tuple[str, int], str | None] = {}


def llama_version(exe: str) -> str | None:
    """`llama-server --version`'s "version: ..." line, once per binary (path and mtime)."""
    try:
        key = (exe, Path(exe).stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _versions:
        env = {**os.environ, "LD_LIBRARY_PATH": os.pathsep.join(
            p for p in (str(Path(exe).parent), os.environ.get("LD_LIBRARY_PATH")) if p)}
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5, env=env)
            text = out.stdout + out.stderr
        except (OSError, subprocess.TimeoutExpired):
            text = ""
        found = re.search(r"^version: *(.+)$", text, re.MULTILINE)
        _versions[key] = found.group(1).strip()[:80] if found else None
    return _versions[key]


def bench_argv(action: str, body: dict, python: str, model_log: Path) -> tuple[list[str], dict]:
    """The `python -m sts_cua.bench` argv for one action and its validated settings.

    settings: "action", "models" (every model the job uses), "serve" (it starts model servers
    itself), "needs_server" (local models that must already be served) and the action's fields.
    """
    settings: dict = {"action": action, "models": [], "serve": False, "needs_server": []}
    if action == "extract":
        n = _number(body, "max_per_run", None, 1, 1000, integer=True)
        args = ["extract"] + (["--max-per-run", str(n)] if n is not None else [])
        settings["max_per_run"] = n
    elif action == "review":
        args = ["review"]
    elif action == "delete":
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > MAX_DELETE:
            raise ApiError(400, f"ids must be a list of 1 to {MAX_DELETE} target ids")
        for i in ids:
            if not isinstance(i, str) or not TARGET_ID.match(i):
                raise ApiError(400, f"target ids must match {TARGET_ID.pattern}")
        ids = list(dict.fromkeys(ids))
        args = ["review", "--delete", *ids]
        settings["ids"] = ids
    elif action == "grounding":
        models = body.get("models")
        if not isinstance(models, list) or not models:
            raise ApiError(400, "models must be a non-empty list")
        for m in models:
            if m not in BENCH_MODELS:
                raise ApiError(400, f"models must be from {', '.join(BENCH_MODELS)}")
        models = list(dict.fromkeys(models))
        if len(models) > MAX_MODELS:
            raise ApiError(400, f"at most {MAX_MODELS} models per run")
        mode = _choice(body, "mode", serve.MODES, "solo")
        serving = _flag(body, "serve")
        limit = _number(body, "limit", None, 1, 100000, integer=True)
        local = [m for m in models if m in MODELS]
        args = ["grounding", *models, "--mode", mode] + (["--serve"] if serving else []) \
            + (["--limit", str(limit)] if limit is not None else [])
        settings.update(models=models, mode=mode, limit=limit, serve=serving and bool(local),
                        needs_server=[] if serving else local)
    elif action == "baseline":
        label = _choice(body, "label", BASELINE_LABELS, "pre-server")
        mode = _choice(body, "mode", (None, *serve.MODES))
        args = ["baseline", "--label", label] + (["--mode", mode] if mode else [])
        settings.update(label=label, mode=mode)
    elif action == "resources":
        model = _choice(body, "model", tuple(MODELS))
        mode = _choice(body, "mode", serve.MODES)
        game = _flag(body, "game")
        serving = _flag(body, "serve")
        args = ["resources", model, "--mode", mode] + (["--game"] if game else []) + (["--serve"] if serving else [])
        if MODELS[model].server == "vllm":  # weights and KV cache from vLLM's startup log, for the activations row
            args += ["--server-log", str(model_log)]
        settings.update(models=[model], mode=mode, game=game, serve=serving, needs_server=[] if serving else [model])
    elif action == "clicks":
        model = _choice(body, "model", BENCH_MODELS)
        n = _number(body, "n", 20, 1, 100, integer=True)
        args = ["clicks", model, "--xvfb", "--display", CLICKS_DISPLAY, "--n", str(n)]
        settings.update(models=[model], n=n, needs_server=[model] if model in MODELS else [])
    elif action == "write_docs":
        args = ["report", "--write-docs", "--docs", DOCS]
    else:
        raise ApiError(404, f"no bench action {action}")
    return [python, "-m", "sts_cua.bench", *args], settings


def install_argv(body: dict, script: Path) -> tuple[list[str], dict]:
    prebuilt = _flag(body, "prebuilt")
    tag = body.get("tag")
    if tag is not None and (not prebuilt or not isinstance(tag, str) or not RELEASE_TAG.match(tag)):
        raise ApiError(400, f"tag goes with prebuilt and must match {RELEASE_TAG.pattern}, e.g. b11900")
    argv = ["bash", str(script)] + (["--prebuilt"] + ([tag] if tag else []) if prebuilt else ["--build"])
    return argv, {"prebuilt": prebuilt, "tag": tag}


def _read_since(path: Path, offset: int, limit: int = OUTPUT_BYTES) -> str:
    """A log's text from offset on, at most its last limit bytes."""
    try:
        with open(path, "rb") as f:
            end = f.seek(0, os.SEEK_END)
            start = max(offset, end - limit)
            f.seek(start)
            text = f.read(end - start).decode("utf-8", "replace")
    except OSError:
        return ""
    return text.split("\n", 1)[-1] if start > offset else text


def _diff(before: str, after: str, limit: int = 200) -> list[str]:
    lines = [line for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
             if not line.startswith(("---", "+++", "@@"))]
    return lines[:limit]


class BenchRoutes:
    """Endpoints of the Bake-off tab and the llama.cpp install, mixed into routes.App."""

    # ---- shared checks

    def _bench_dir(self) -> Path:
        return self.config.repo / "bench"

    def _last_file(self, name: str) -> Path:
        return self.config.state_dir / f"{name}_last.json"

    def _served_model(self) -> tuple[bool, str | None]:
        """(a model server is running, the registry model it serves if known)."""
        if not self.procs.info(serve.NAME)["running"]:
            return False, None
        record = self.procs.managed(serve.NAME) or {}
        return True, record.get("model") or serve.registry_name(self.served(self.config.model_port))

    def _model_starting(self) -> bool:
        record = self.procs.managed(serve.NAME)
        return bool(record and "ready_s" not in record
                    and not self.probe(record.get("server", "llama"), record.get("port", self.config.model_port)))

    def _bench_serving(self) -> bool:
        """A bench job that starts model servers itself is running (or one started elsewhere: can't tell)."""
        if not self.procs.info(BENCH)["running"]:
            return False
        record = self.procs.managed(BENCH)
        return record is None or bool(record.get("serve"))

    def _agent_on_local_model(self) -> str | None:
        for name, what in (("supervisor", "a supervisor"), ("coop", "a co-op session")):
            if self.procs.info(name)["running"]:
                model = ((self.procs.managed(name) or {}).get("settings") or {}).get("model")
                if model is None or model in MODELS:
                    return what
        return None

    def _check_bench(self, settings: dict) -> None:
        """409 when this job would collide with what is running. Called under self._lock."""
        p = self.procs
        if p.managed("stack_down"):
            raise ApiError(409, "the stack is being stopped")
        if p.info(BENCH)["running"]:
            record = p.managed(BENCH) or {}
            raise ApiError(409, f"a bench job is already running ({record.get('action', 'started elsewhere')}); "
                                "stop it or wait")
        if settings["serve"]:
            if self._model_starting():
                raise ApiError(409, "the model server is starting; wait until it answers")
            if p.info(INSTALL)["running"]:
                raise ApiError(409, "llama.cpp is being installed; wait for it to finish")
            agent = self._agent_on_local_model()
            if agent:
                raise ApiError(409, f"{agent} is running on a local model; starting servers would stop its model")
            if settings["action"] == "resources" and p.info(serve.NAME)["running"]:
                raise ApiError(409, "a model server is already running: stop it first, or measure it as it is "
                                    "(untick 'start it for me')")
        if settings["action"] == "baseline":
            if p.info(serve.NAME)["running"]:
                raise ApiError(409, "a baseline is taken with no model server running; stop it first")
            if settings["label"] == "desktop" and p.info("game")["running"]:
                raise ApiError(409, "the desktop baseline is taken before the games start; Spire 2 is running")
        needed = list(dict.fromkeys(settings["needs_server"]))
        if needed:
            running, current = self._served_model()
            if len(needed) > 1:
                raise ApiError(409, "only one model server runs at a time: tick 'start each model for me' to test "
                                    "several local models")
            if not running:
                raise ApiError(409, f"no model server is running: start {needed[0]} in Control first"
                               + (", or tick 'start each model for me'" if settings["action"] == "grounding" else ""))
            if current and current != needed[0]:
                raise ApiError(409, f"the model server is serving {current}, not {needed[0]}")

    def _start_job(self, name: str, argv: list[str], settings: dict, env: dict | None = None) -> int:
        pid = self.procs.start(name, argv, cwd=self.config.repo, env=env, extra=settings)
        record = self.procs.managed(name) or {}
        last = {**settings, "argv": argv, "pid": pid, "started_at": record.get("started_at"),
                "log": record.get("log", str(self.procs.log_path(name))), "log_offset": record.get("log_offset", 0)}
        self._last_file(name).write_text(json.dumps(last) + "\n", encoding="utf-8")
        return pid

    def _last(self, name: str, lines: int) -> dict | None:
        """The newest job's settings, whether it still runs, its exit code (if this app saw it end) and output."""
        path = self._last_file(name)
        last = files.read_json(path)
        if not last:
            return None
        record = self.procs.managed(name)
        running = bool(record and record.get("pid") == last.get("pid"))
        if not running and "exit_code" not in last and name in self.procs.exits:
            last["exit_code"] = self.procs.exits[name]
            path.write_text(json.dumps(last) + "\n", encoding="utf-8")
        text = _read_since(Path(last.get("log") or self.procs.log_path(name)), int(last.get("log_offset") or 0))
        out = {k: last.get(k) for k in ("action", "argv", "pid", "started_at", "models", "mode", "serve",
                                        "prebuilt", "tag")}
        out.update(running=running, exit_code=last.get("exit_code"), output=text.splitlines()[-lines:])
        if last.get("action") == "write_docs":
            touched = next((m.group(1) for m in map(_TOUCHED.search, reversed(text.splitlines())) if m), None)
            out["changed"] = None
            if touched is not None:
                before = self.config.state_dir / "bench_docs_before.md"
                try:
                    after = (self.config.repo / DOCS).read_text(encoding="utf-8")
                    diff = _diff(before.read_text(encoding="utf-8"), after)
                except OSError:
                    diff = []
                rows = [] if touched.strip() == "none" else [t.strip() for t in touched.split(",") if t.strip()]
                out["changed"] = {"rows": rows, "diff": diff}
        return out

    # ---- llama.cpp

    def llama(self, query: dict, **_) -> dict:
        exe = serve.find_llama_server(self.config.llama_server)
        info = self.procs.info(INSTALL)
        last = self._last(INSTALL, INSTALL_TAIL)
        release = None
        if exe:
            try:
                release = (Path(exe).parent / "RELEASE").read_text(encoding="utf-8").strip()[:20] or None
            except OSError:
                pass
        tail = (last or {}).get("output") or []
        return {
            "found": bool(exe), "path": exe, "version": llama_version(exe) if exe else None, "release": release,
            "install": {"running": info["running"], "pid": info["pid"], "since": info["since"],
                        "prebuilt": (last or {}).get("prebuilt"), "exit_code": (last or {}).get("exit_code"),
                        "started_at": (last or {}).get("started_at"), "tail": tail,
                        "nvcc_missing": any("nvcc not found" in line for line in tail)},
        }

    def llama_install(self, body: dict, **_) -> dict:
        argv, settings = install_argv(body, self.config.repo / "tools" / "install_llama_cpp.sh")
        with self._lock:
            if self.procs.managed("stack_down"):
                raise ApiError(409, "the stack is being stopped")
            if self.procs.info(INSTALL)["running"]:
                raise ApiError(409, "llama.cpp is already being installed")
            if self.procs.info(serve.NAME)["running"]:
                raise ApiError(409, "a model server is running (llama-server can't be replaced while it runs); "
                                    "stop it first")
            if self._bench_serving():
                raise ApiError(409, "a bench job that starts model servers is running; stop it or wait")
            pid = self._start_job(INSTALL, argv, {"action": "install", **settings})
        return {"started": True, "pid": pid, "argv": argv}

    # ---- bake-off

    def bench(self, query: dict, **_) -> dict:
        from ..bench import resources
        from ..bench import targets as T

        lines = _query_int(query, "lines", 200, 1, 2000)
        bench_dir = self._bench_dir()
        targets: dict = {"count": 0, "rejected": 0, "items": [], "sheets": [], "sheets_stale": False, "error": None}
        try:
            loaded = T.load_targets(bench_dir)
            targets["count"] = len(loaded)
            targets["items"] = [{"id": t.id, "instruction": t.instruction, "state_type": t.state_type,
                                 "check": t.check} for t in loaded[:MAX_TARGET_ROWS]]
            targets["rejected"] = len(T.rejected(bench_dir))
        except (OSError, ValueError, KeyError, TypeError) as e:
            targets["error"] = f"can't read {bench_dir / T.TARGETS_FILE}: {e}"
        sheets = sorted(p for p in (bench_dir / "review").glob("sheet-*.png") if SHEET.match(p.name))
        targets["sheets"] = [{"name": p.name, "url": f"/api/bench/review/{p.name}?v={p.stat().st_mtime_ns}"}
                             for p in sheets]
        targets_file = bench_dir / T.TARGETS_FILE
        if targets_file.exists() and targets["count"]:
            newest = max((p.stat().st_mtime for p in sheets), default=0.0)
            targets["sheets_stale"] = newest < targets_file.stat().st_mtime
        info = self.procs.info(BENCH)
        record = self.procs.managed(BENCH) or {}
        running, current = self._served_model()
        root = self._models_root()
        have = serve.downloaded_names(root)
        baselines = {k: {"time": v.get("time"), "mode": v.get("mode"),
                         "gpu_used_mib": (v.get("gpu") or {}).get("used_mib"),
                         "ram_used_mib": (v.get("ram") or {}).get("used_mib")}
                     for k, v in resources.load_baselines(self.config.runs).items() if isinstance(v, dict)}
        return {
            "job": {**info, "action": record.get("action") if record else (None if not info["running"] else "external"),
                    "external": info["running"] and not record},
            "last": self._last(BENCH, lines),
            "targets": targets,
            "models": {
                "local": [{"name": m.name, "server": m.server, "thinking": m.thinking, "rank": m.rank,
                           "need_gib": round(m.need_gib, 2), "downloaded": m.name in have,
                           "fits": {mode: m.fits(budget(mode)) for mode in serve.MODES}}
                          for m in sorted(MODELS.values(), key=lambda m: -m.rank)],
                "reference": list(REFERENCE_MODELS),
            },
            "modes": list(serve.MODES),
            "baselines": baselines,
            "server": {"running": running, "model": current,
                       "mode": (self.procs.managed(serve.NAME) or {}).get("mode") if running else None},
            "clicks_display": CLICKS_DISPLAY,
        }

    def bench_start(self, action: str, body: dict, **_) -> dict:
        action = action.replace("-", "_")
        argv, settings = bench_argv(action, body, self.python, self.procs.log_path(serve.NAME))
        if action == "delete":
            from ..bench import targets as T

            known = {t.id for t in T.load_targets(self._bench_dir())}
            unknown = [i for i in settings["ids"] if i not in known]
            if unknown:
                raise ApiError(400, f"no target {unknown[0]}" + (f" (and {len(unknown) - 1} more)" if len(unknown) > 1 else ""))
        env = {"PYTHONUNBUFFERED": "1", serve.MODELS_ENV: str(self._models_root())}
        exe = serve.find_llama_server(self.config.llama_server)
        if exe:
            env["LLAMA_SERVER"] = exe  # what `python -m sts_cua.serve start` (--serve) runs
        with self._lock:
            self._check_bench(settings)
            if action == "write_docs":  # to show which lines the report changed
                docs = self.config.repo / DOCS
                (self.config.state_dir / "bench_docs_before.md").write_text(
                    docs.read_text(encoding="utf-8") if docs.exists() else "", encoding="utf-8")
            pid = self._start_job(BENCH, argv, settings, env)
        return {"started": True, "pid": pid, "action": action, "argv": argv}

    def bench_stop(self, **_) -> dict:
        return {"stopped": True, "pids": self.procs.stop(BENCH)}

    def bench_sheet(self, file: str, **_) -> Reply:
        if not SHEET.match(_name(file, "file")):
            raise ApiError(404, "contact sheets are bench/review/sheet-NN.png")
        path = files.inside(self._bench_dir() / "review", file)
        if path is None or not path.is_file():
            raise ApiError(404, f"no contact sheet {file}")
        return Reply(path.read_bytes(), "image/png")

    def bench_results(self, **_) -> dict:
        """The newest result per model (per model and mode for resources), read from runs/bench/*/results.json."""
        from ..bench import report

        data = report.latest(clean_results(load_results(self.config.runs)))
        local = set(MODELS)

        def when(r: dict) -> str | None:
            return r.get("created_at")

        grounding = []
        for model, e in sorted(data["grounding"].items()):
            s = e.get("summary") or {}
            grounding.append({"model": model, "local": model in local, "created_at": when(e),
                              "game_build": e.get("game_build"), "targets": e.get("targets"),
                              **{k: s.get(k) for k in ("hit_rate", "hits", "answered", "errors", "malformed",
                                                       "median_error_px", "latency_p50_s", "latency_p95_s",
                                                       "tokens_per_s")},
                              "dir": Path(e.get("dir") or "").name or None})
        clicks = []
        for model, c in sorted(data["clicks"].items()):
            s = c.get("summary") or {}
            clicks.append({"model": model, "local": model in local, "created_at": when(c),
                           **{k: s.get(k) for k in ("hits", "answered", "malformed", "errors", "latency_p50_s",
                                                    "latency_p95_s", "median_error_px")}})
        res = []
        for (model, mode), r in sorted(data["resources"].items()):
            game = r.get("game") or {}
            fits, need, how = report.fit(model, mode, data["resources"])
            res.append({"model": model, "mode": mode, "created_at": when(r), "vram_used_gib": r.get("vram_used_gib"),
                        "vram_total_gib": r.get("vram_total_gib"), "model_vram_gib": r.get("model_vram_gib"),
                        "budget_gib": budget(mode), "fits": fits,
                        "server_rss_gib": (r.get("server") or {}).get("rss_gib"),
                        "ram_used_gib": r.get("ram_used_gib"),
                        "game_rss_gib": game.get("rss_gib") if game.get("running") else None})
        try:
            ranked = report.ranking(data)
            recommended = report.recommended(ranked)
            markdown = report.block(data)
        except (KeyError, TypeError, ValueError, AttributeError):
            ranked, recommended, markdown = [], {"coop": None, "solo": None}, None
        ranking = [{"model": r["model"], "hit_rate": r["hit_rate"], "latency_p50_s": r["latency_p50_s"],
                    "local": r["local"],
                    "fits": {mode: {"fits": f[0], "need_gib": f[1], "how": f[2]} for mode, f in r["fits"].items()}}
                   for r in ranked]
        return {"grounding": grounding, "clicks": clicks, "resources": res, "ranking": ranking,
                "recommended": recommended, "markdown": markdown}


def load_results(runs: Path) -> list[dict]:
    """Like bench.results.load_all (oldest first, `dir` set), skipping files that aren't a JSON object."""
    from ..bench import results as R

    out = []
    for path in sorted(R.bench_runs(runs).glob(f"*/{R.RESULTS_FILE}")):
        data = files.read_json(path)
        if data is not None:
            out.append({**data, "dir": str(path.parent)})
    out.sort(key=lambda r: str(r.get("created_at") or ""))
    return out


def clean_results(results: list[dict]) -> list[dict]:
    """Bench results with the fields report.latest reads; anything malformed is left out, not a 500."""
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        kind = r.get("kind")
        if kind == "grounding":
            models = [m for m in r.get("models") or [] if isinstance(m, dict) and isinstance(m.get("model"), str)
                      and isinstance(m.get("summary"), dict)]
            out.append({**r, "models": models})
        elif kind == "clicks" and isinstance(r.get("model"), str) and isinstance(r.get("summary"), dict):
            out.append(r)
        elif kind == "resources" and isinstance(r.get("model"), str) and r.get("mode") in serve.MODES:
            out.append(r)
    return out


def stop_jobs(procs) -> None:
    """Stack down: the bake-off job and the llama.cpp install, whole process groups."""
    procs.stop(BENCH)
    procs.stop(INSTALL, first=signal.SIGTERM, then=signal.SIGKILL)
