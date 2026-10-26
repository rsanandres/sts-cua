"""Serve the local models in models/registry.py: list, download, start, stop, status.

    python -m sts_cua.serve list
    python -m sts_cua.serve download holo4-27b
    python -m sts_cua.serve start holo4-27b --mode solo
    python -m sts_cua.serve start best --mode coop     # strongest downloaded model that fits
    python -m sts_cua.serve status
    python -m sts_cua.serve stop

One model server at a time, on 127.0.0.1:8000, where --base-url points by default.
llama.cpp models (server "llama") run with llama-server; the vLLM baseline with
`vllm serve`. The server is started in its own session with a pid file in
~/.sts-cua/run/model.pid and its log in ~/.sts-cua/logs/model.log, the same files the
control app uses, so either can stop what the other started.

Files: llama.cpp models download into <models dir>/<repo>/ (the registry's weights and
mmproj, nothing else); the vLLM baseline goes into the Hugging Face cache, where
`vllm serve <repo>` looks for it (and fetches it itself on first start if missing).
The models dir is --models-dir, else $STS_CUA_MODELS, else ~/models, or /mnt/d/models
when only that exists (a Windows drive with room for several 27B models). Weights on a
Windows drive (/mnt/...) load through WSL's drvfs, which is slower than the Linux
filesystem, so llama-server reads them without mmap there. Each load time goes to
logs/model.log and logs/model_loads.jsonl, for comparing the two.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .app.procs import ProcessManager
from .models.registry import GIB, MODELS, File, LocalModel, best, budget

HOST = "127.0.0.1"
PORT = 8000
MODES = ("coop", "solo")
BEST = "best"
NAME = "model"  # pid file run/model.pid, log logs/model.log
NVIDIA_SMI = ("/usr/lib/wsl/lib/nvidia-smi", "nvidia-smi")
START_TIMEOUT = 900.0  # seconds; a 27B model read from /mnt/c can take minutes
HF_BASE = "https://huggingface.co"
MODELS_ENV = "STS_CUA_MODELS"
WINDOWS_MODELS = Path("/mnt/d/models")  # Raph's D: drive, ~1 TB free
CHUNK = 8 << 20


class ServeError(Exception):
    pass


# ---- where the files live

def models_root(root: Path | str | None = None, windows: Path = WINDOWS_MODELS) -> Path:
    """--models-dir, else $STS_CUA_MODELS, else ~/models, else /mnt/d/models if only it exists."""
    if root:
        return Path(root).expanduser()
    if os.environ.get(MODELS_ENV):
        return Path(os.environ[MODELS_ENV]).expanduser()
    home = Path.home() / "models"
    return windows if not home.exists() and windows.is_dir() else home


def on_drvfs(path: Path) -> bool:
    """A Windows drive mounted in WSL (/mnt/c, /mnt/d, ...), symlinks followed."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return resolved.parts[:2] == ("/", "mnt") and len(resolved.parts) > 2 and len(resolved.parts[2]) == 1


def disk_free(path: Path) -> int:
    return shutil.disk_usage(_existing(path)).free


def hub_cache() -> Path:
    """The Hugging Face hub cache: $HF_HUB_CACHE, else $HF_HOME/hub, else ~/.cache/huggingface/hub."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    home = os.environ.get("HF_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".cache" / "huggingface") / "hub"


def model_dir(m: LocalModel, root: Path | None = None) -> Path:
    """Where the model's files go: ~/models/<repo>/ for llama.cpp, the hub cache repo folder for vLLM."""
    if m.server == "vllm":
        return hub_cache() / ("models--" + m.repo.replace("/", "--"))
    return models_root(root) / m.repo


def files_of(m: LocalModel) -> list[File]:
    return [*m.weights, *([m.mmproj] if m.mmproj else [])]


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def find_file(m: LocalModel, f: File, root: Path | None = None) -> Path | None:
    """The file on disk if it is complete (exactly the registry's size), else None."""
    d = model_dir(m, root)
    candidates = sorted(d.glob(f"snapshots/*/{f.name}")) if m.server == "vllm" else [d / f.name]
    return next((p for p in candidates if _size(p) == f.size), None)


def _partial_bytes(m: LocalModel, f: File, root: Path | None = None) -> int:
    """Bytes of an unfinished HTTPS download of f (its .part file)."""
    if m.server == "vllm":
        return 0
    return min(_size(model_dir(m, root) / (f.name + ".part")), f.size)


def _incomplete_bytes(m: LocalModel, root: Path | None = None) -> int:
    """Bytes in hf's unfinished downloads: <dir>/.cache/huggingface/download/**/*.incomplete with
    --local-dir, blobs/*.incomplete in the hub cache. Named by hash, so not matched to a file."""
    d = model_dir(m, root)
    pattern = "blobs/*.incomplete" if m.server == "vllm" else ".cache/huggingface/download/**/*.incomplete"
    return sum(_size(p) for p in d.glob(pattern))


def downloaded(m: LocalModel, root: Path | None = None) -> bool:
    return all(find_file(m, f, root) for f in files_of(m))


def downloaded_names(root: Path | None = None) -> set[str]:
    return {name for name, m in MODELS.items() if downloaded(m, root)}


def progress(m: LocalModel, root: Path | None = None) -> dict:
    """{"bytes", "total"} on disk for the model's files, counting unfinished downloads."""
    total = sum(f.size for f in files_of(m))
    have = sum(f.size if find_file(m, f, root) else _partial_bytes(m, f, root) for f in files_of(m))
    return {"bytes": min(total, have + _incomplete_bytes(m, root)), "total": total}


# ---- download

Fetcher = Callable[[LocalModel, File, Path], None]


def hf_exe() -> str | None:
    """The huggingface_hub CLI: next to this interpreter (the venv), else on PATH."""
    venv = Path(sys.executable).parent / "hf"
    return str(venv) if venv.exists() else shutil.which("hf")


def hf_fetcher(exe: str) -> Fetcher:
    """`hf download <repo> <file> --local-dir <dir>`; resumable, see the huggingface_hub CLI guide.
    For the vLLM baseline, `hf download <repo>` into the hub cache (the whole repo, which vLLM needs)."""
    def fetch(m: LocalModel, f: File, dest: Path) -> None:
        argv = [exe, "download", m.repo] if m.server == "vllm" else [exe, "download", m.repo, f.name,
                                                                     "--local-dir", str(dest)]
        if subprocess.run(argv).returncode != 0:
            raise ServeError(f"{' '.join(argv)} failed")
    return fetch


def https_fetch(m: LocalModel, f: File, dest: Path, opener=urllib.request.urlopen) -> None:
    """Plain HTTPS from the repo's resolve URL into <file>.part, resuming with a Range request,
    renamed into place once it has the registry's size. $HF_TOKEN is sent if set."""
    if m.server == "vllm":
        raise ServeError(f"{m.name}: install huggingface_hub (hf) to download it, or let vllm serve fetch it on start")
    dest.mkdir(parents=True, exist_ok=True)
    final, part = dest / f.name, dest / (f.name + ".part")
    url = f"{HF_BASE}/{m.repo}/resolve/main/{urllib.parse.quote(f.name)}"
    start = _size(part)
    if start > f.size:
        part.unlink()
        start = 0
    if start < f.size:
        headers = {"User-Agent": "sts-cua"}
        if os.environ.get("HF_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
        if start:
            headers["Range"] = f"bytes={start}-"
        with opener(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            mode = "ab" if start and getattr(resp, "status", 200) == 206 else "wb"
            with part.open(mode) as out:
                while chunk := resp.read(CHUNK):
                    out.write(chunk)
    if _size(part) == f.size:
        part.replace(final)


def default_fetcher() -> Fetcher:
    exe = hf_exe()
    return hf_fetcher(exe) if exe else https_fetch


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def download(m: LocalModel, root: Path | None = None, fetcher: Fetcher | None = None,
             disk_free: Callable[[Path], int] = disk_free,
             out: Callable[[str], None] = print) -> Path:
    """Fetch the registry's files for m (only those not already complete), then check every size."""
    dest = model_dir(m, root)
    missing = [f for f in files_of(m) if not find_file(m, f, root)]
    need = sum(f.size - _partial_bytes(m, f, root) for f in missing)
    free = disk_free(dest)
    out(f"{m.name}: {len(missing)} of {len(files_of(m))} files to fetch into {dest}; "
        f"{need / GIB:.2f} GiB needed, {free / GIB:.2f} GiB free")
    if need > free:
        raise ServeError(f"not enough disk: {need / GIB:.2f} GiB needed, {free / GIB:.2f} GiB free on {_existing(dest)}")
    fetch = fetcher or default_fetcher()
    if m.server == "vllm" and missing:
        missing = missing[:1]  # one `hf download <repo>` fetches the whole repo
    for f in missing:
        out(f"fetching {m.repo}/{f.name} ({f.size / GIB:.2f} GiB)")
        fetch(m, f, dest)
    bad = []
    for f in files_of(m):
        if not find_file(m, f, root):
            path = dest / f.name
            bad.append(f"{f.name}: expected {f.size} bytes, have {_size(path) if path.exists() else 'none'}")
    if bad:
        raise ServeError("size check failed: " + "; ".join(bad))
    out(f"{m.name}: all {len(files_of(m))} files present with the registry's sizes")
    return dest


# ---- GPU and choosing a model

def gpu(commands=NVIDIA_SMI) -> dict | None:
    """{"used_mib", "free_mib", "total_mib"} of the first GPU, or None if nvidia-smi can't be read."""
    for exe in commands:
        try:
            out = subprocess.run([exe, "--query-gpu=memory.used,memory.free,memory.total",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            used, free, total = (int(float(x)) for x in out.stdout.splitlines()[0].split(","))
        except (OSError, subprocess.TimeoutExpired, IndexError, ValueError):
            continue
        if out.returncode == 0:
            return {"used_mib": used, "free_mib": free, "total_mib": total}
    return None


def free_gib(reading: dict | None) -> float | None:
    return None if reading is None else reading["free_mib"] / 1024


def choose(name: str, mode: str, free: float | None, have: set[str], force: bool = False) -> tuple[LocalModel, str]:
    """The model to start and why. `name` is a registry name or "best": the highest-ranked
    downloaded model whose need fits min(free VRAM, the mode's budget). An explicit model
    must fit both too, unless force. Raises ServeError with the reason otherwise."""
    if mode not in MODES:
        raise ServeError(f"mode must be one of {', '.join(MODES)}")
    cap = budget(mode)
    limit = cap if free is None else min(free, cap)
    where = (f"the {mode} budget of {cap:.1f} GiB (free VRAM unknown)" if free is None
             else f"min(free VRAM {free:.1f} GiB, {mode} budget {cap:.1f} GiB)")
    if name == BEST:
        if free is None and not force:
            raise ServeError(f"can't read free VRAM (nvidia-smi); pass --force to pick by the {mode} budget alone")
        m = best(limit, have)
        stronger = sorted((o for o in MODELS.values() if m is None or o.rank > m.rank), key=lambda o: -o.rank)
        skipped = [f"{o.name} ({'not downloaded' if o.name not in have else f'needs {o.need_gib:.1f} GiB'})"
                   for o in stronger]
        if m is None:
            raise ServeError(f"no downloaded model fits {limit:.1f} GiB = {where}; "
                             f"downloaded: {', '.join(sorted(have)) or 'none'}")
        reason = f"best that fits: {m.name} (rank {m.rank}, needs {m.need_gib:.1f} GiB of {limit:.1f} GiB = {where})"
        return m, reason + (f"; skipped stronger: {', '.join(skipped)}" if skipped else "")
    m = MODELS.get(name)
    if m is None:
        raise ServeError(f"unknown model {name}; choose from {BEST}, {', '.join(MODELS)}")
    if m.server == "llama" and name not in have:
        raise ServeError(f"{name} isn't downloaded; run python -m sts_cua.serve download {name}")
    problems = []
    if m.need_gib > cap:
        problems.append(f"needs {m.need_gib:.1f} GiB, over the {mode} budget of {cap:.1f} GiB")
    if free is None:
        problems.append("can't read free VRAM (nvidia-smi)")
    elif m.need_gib > free:
        problems.append(f"needs {m.need_gib:.1f} GiB but only {free:.1f} GiB of VRAM is free")
    if problems and not force:
        raise ServeError(f"{name}: {'; '.join(problems)}; pass --force to start anyway")
    reason = f"{m.name} (rank {m.rank}, needs {m.need_gib:.1f} GiB of {limit:.1f} GiB = {where})"
    return m, reason + (f"; forced past: {'; '.join(problems)}" if problems else "")


# ---- server command lines

def find_llama_server(explicit: str | None = None) -> str | None:
    """--llama-server, $LLAMA_SERVER, ~/llama.cpp/build/bin/llama-server (tools/install_llama_cpp.sh), PATH."""
    for candidate in (explicit, os.environ.get("LLAMA_SERVER"), str(Path.home() / "llama.cpp/build/bin/llama-server")):
        if candidate and os.access(candidate, os.X_OK) and Path(candidate).is_file():
            return candidate
        if candidate and candidate == explicit:
            return None  # an explicit path that doesn't exist is an error, not a fallback
    return shutil.which("llama-server")


def find_vllm(explicit: str | None = None, repo: Path | None = None) -> str | None:
    """--vllm, <repo>/.venv/bin/vllm, vllm next to this interpreter, PATH."""
    if explicit:
        return explicit
    for venv in ([repo / ".venv" / "bin" / "vllm"] if repo else []) + [Path(sys.executable).parent / "vllm"]:
        if venv.exists():
            return str(venv)
    return shutil.which("vllm")


def vllm_flags(m: LocalModel) -> list[str]:
    """docs/hosting.md ("Current adapter"): vLLM refuses to start at its default 0.92 on this PC."""
    return ["--gpu-memory-utilization", "0.55", "--max-model-len", str(m.context), "--max-num-seqs", "1",
            "--limit-mm-per-prompt", '{"image":1,"video":0}']


def _help(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout + out.stderr


def no_mmap_flags(help_text: str) -> list[str]:
    """Read the weights without mmap. Current llama-server: `-lm, --load-mode none` ("no special
    loading mode"; the default auto means mmap). Older builds had `--no-mmap` instead."""
    if "--load-mode" not in help_text and "--no-mmap" in help_text:
        return ["--no-mmap"]
    return ["--load-mode", "none"]


def load_flags(m: LocalModel, exe: str, root: Path | None = None, help_text: str | None = None) -> list[str]:
    """Extra llama-server flags for where the weights are: no mmap on a Windows drive (drvfs)."""
    if m.server != "llama" or not on_drvfs(model_dir(m, root) / m.weights[0].name):
        return []
    return no_mmap_flags(_help(exe) if help_text is None else help_text)


def server_argv(m: LocalModel, exe: str, root: Path | None = None, port: int = PORT,
                help_text: str | None = None) -> list[str]:
    """The server argv for m. llama-server flags, checked against tools/server/README.md in
    ggml-org/llama.cpp (master, 2026-10-26):
      -m/--model, -mm/--mmproj (vision projector), -c/--ctx-size, -ngl/--n-gpu-layers
      (a number, 'auto' or 'all'; 999 means every layer), -fa/--flash-attn on|off|auto,
      --jinja (chat template engine; on by default now, passed for older builds),
      -np/--parallel (slots; one AI seat), --host, --port (default is no longer 8080),
      -a/--alias (the model id /v1/models answers, which --served-model points at),
      -lm/--load-mode none on a Windows drive (load_flags).
    A split GGUF loads from its first part, so weights[0] is passed."""
    if m.server == "llama":
        d = model_dir(m, root)
        argv = [exe, "-m", str(d / m.weights[0].name)]
        if m.mmproj:
            argv += ["--mmproj", str(d / m.mmproj.name)]
        return [*argv, "-c", str(m.context), "-ngl", "999", "-fa", "on", "--jinja", "-np", "1",
                "--host", HOST, "--port", str(port), "--alias", m.served_name or m.name,
                *load_flags(m, exe, root, help_text), *m.extra_args]
    argv = [exe, "serve", m.repo, *vllm_flags(m), "--host", HOST, "--port", str(port)]
    if m.served_name and m.served_name != m.repo:
        argv += ["--served-model-name", m.served_name]
    return [*argv, *m.extra_args]


def ready_path(server: str) -> str:
    """llama-server answers /health 200 once the model is loaded (503 while loading); vLLM's
    /v1/models answers once its API server is up."""
    return "/health" if server == "llama" else "/v1/models"


def _get(port: int, path: str, timeout: float) -> tuple[int, bytes] | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}{path}", timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (OSError, ValueError):
        return None


def ready(server: str, port: int = PORT, timeout: float = 1.0) -> bool:
    got = _get(port, ready_path(server), timeout)
    return got is not None and got[0] == 200


def parse_models(body: bytes | str) -> list[str]:
    """Model ids from an OpenAI /v1/models reply."""
    try:
        data = json.loads(body)
        return [str(d["id"]) for d in data.get("data", []) if isinstance(d, dict) and "id" in d]
    except (ValueError, AttributeError, TypeError):
        return []


def served(port: int = PORT, timeout: float = 1.0) -> list[str] | None:
    """What the server on port serves (/v1/models ids), or None if nothing answers."""
    got = _get(port, "/v1/models", timeout)
    return parse_models(got[1]) if got and got[0] == 200 else None


def registry_name(ids: list[str] | None) -> str | None:
    """The registry model a served id belongs to (its served_name, the repo id for vLLM, or a llama.cpp file path)."""
    for i in ids or []:
        for m in MODELS.values():
            if i in (m.served_name, m.name, m.repo) or i.endswith("/" + m.weights[0].name):
                return m.name
    return None


def port_open(port: int = PORT, host: str = HOST) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


# ---- start, stop, status

def default_state_dir() -> Path:
    return Path.home() / ".sts-cua"


def launch(procs: ProcessManager, m: LocalModel, mode: str, exe: str, root: Path | None = None,
           port: int = PORT, reason: str = "", help_text: str | None = None) -> int:
    """Start m's server as the `model` child: pid file run/model.pid, log logs/model.log."""
    argv = server_argv(m, exe, root, port, help_text)
    weights = model_dir(m, root) / m.weights[0].name if m.server == "llama" else None
    env = {}
    if m.server == "llama":  # prebuilt releases keep their libggml/libcudart .so files next to the binary
        env["LD_LIBRARY_PATH"] = os.pathsep.join(p for p in (str(Path(exe).parent), os.environ.get("LD_LIBRARY_PATH")) if p)
    return procs.start(NAME, argv, cwd=Path.home(), env=env, log_name=NAME,
                       extra={"model": m.name, "mode": mode, "port": port, "server": m.server,
                              "served_name": m.served_name, "reason": reason, "t0": time.time(),
                              "weights": str(weights) if weights else None,
                              "drvfs": bool(weights and on_drvfs(weights))})


def record_load(procs: ProcessManager, seconds: float) -> dict | None:
    """Note how long the running server took to answer: a line in logs/model.log, a JSON line in
    logs/model_loads.jsonl (for the bench) and "ready_s" in the pid file. Once per server."""
    record = procs.managed(NAME)
    if not record or "ready_s" in record:
        return None
    argv = record.get("argv") or []
    flags = "--no-mmap" in argv or any(a == "--load-mode" and b == "none" for a, b in zip(argv, argv[1:]))
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": record.get("model"),
             "mode": record.get("mode"), "server": record.get("server"), "seconds": round(seconds, 1),
             "weights": record.get("weights"), "drvfs": record.get("drvfs", False),
             "load": "no-mmap" if flags else "default (mmap)"}
    procs.update(NAME, ready_s=entry["seconds"])
    with procs.log_path(NAME).open("a", encoding="utf-8") as log:
        log.write(f"--- {entry['time']} sts_cua.serve: {entry['model']} ready after {entry['seconds']} s "
                  f"(weights {entry['weights'] or 'hub cache'}, load {entry['load']})\n")
    with (procs.log_dir / "model_loads.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def watch_load(procs: ProcessManager, server: str, port: int = PORT, timeout: float = START_TIMEOUT,
               probe: Callable[[str, int], bool] | None = None) -> str:
    """Wait for the server like wait_ready, then record its load time (for the app's background thread)."""
    record = procs.managed(NAME) or {}
    result = wait_ready(procs, server, port, timeout, probe or ready)
    if result == "ready":
        record_load(procs, time.time() - record.get("t0", time.time()))
    return result


def wait_ready(procs: ProcessManager, server: str, port: int = PORT, timeout: float = START_TIMEOUT,
               probe: Callable[[str, int], bool] = ready, poll: float = 1.0,
               clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> str:
    """"ready", "exited" (the server died; see its log) or "timeout" (still loading)."""
    deadline = clock() + timeout
    while True:
        if probe(server, port):
            return "ready"
        if procs.managed(NAME) is None:
            return "exited"
        if clock() >= deadline:
            return "timeout"
        sleep(poll)


def stop_server(procs: ProcessManager, grace: float = 10.0, poll: float = 0.2) -> list[int]:
    """SIGINT, then SIGTERM after grace seconds, then SIGKILL; waits for the server to exit."""
    sent = procs.send(NAME, signal.SIGINT)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + grace
        while procs.pids(NAME) and time.monotonic() < deadline:
            time.sleep(poll)
        if not procs.pids(NAME):
            break
        procs.send(NAME, sig)
    return sent


def status_info(procs: ProcessManager, port: int = PORT, reading: dict | None = None,
                ids: list[str] | None = None) -> dict:
    record = procs.managed(NAME) or {}
    return {
        "running": bool(procs.pids(NAME)), "pids": procs.pids(NAME),
        "model": record.get("model") or registry_name(ids), "mode": record.get("mode"),
        "served": ids, "port": record.get("port", port), "gpu": reading,
    }


# ---- CLI

def _fits(m: LocalModel, mode: str) -> str:
    return "yes" if m.fits(budget(mode)) else "no"


def cmd_list(args) -> int:
    root = models_root(args.models_dir)
    have = downloaded_names(root)
    print(f"models dir {root} ({disk_free(root) / GIB:.1f} GiB free"
          f"{', a Windows drive: loads without mmap' if on_drvfs(root) else ''})")
    print(f"{'model':18} {'server':6} {'rank':>4} {'file GiB':>8} {'need GiB':>8} {'co-op 13':>8} {'solo 20':>7}  downloaded")
    for m in sorted(MODELS.values(), key=lambda m: -m.rank):
        print(f"{m.name:18} {m.server:6} {m.rank:>4} {m.file_gib:>8.2f} {m.need_gib:>8.2f} {_fits(m, 'coop'):>8} "
              f"{_fits(m, 'solo'):>7}  {'yes' if m.name in have else 'no'}")
    free = free_gib(gpu())
    if free is not None:
        picks = {mode: best(min(free, budget(mode)), have) for mode in MODES}
        print(f"free VRAM now {free:.1f} GiB; best downloaded: "
              + ", ".join(f"{mode} {p.name if p else 'none'}" for mode, p in picks.items()))
    return 0


def cmd_download(args) -> int:
    m = MODELS.get(args.model)
    if m is None:
        raise ServeError(f"unknown model {args.model}; choose from {', '.join(MODELS)}")
    download(m, models_root(args.models_dir))
    return 0


def cmd_start(args) -> int:
    procs = ProcessManager(args.state_dir)
    root = models_root(args.models_dir)
    if procs.pids(NAME):
        raise ServeError(f"a model server is already running (pid {procs.pids(NAME)[0]}); stop it first")
    if port_open(args.port):
        raise ServeError(f"something already answers on port {args.port}; stop it first")
    m, reason = choose(args.model, args.mode, free_gib(gpu()), downloaded_names(root), args.force)
    print(reason)
    exe = find_llama_server(args.llama_server) if m.server == "llama" else find_vllm(args.vllm)
    if not exe:
        raise ServeError("llama-server not found: run tools/install_llama_cpp.sh, set $LLAMA_SERVER or pass "
                         "--llama-server" if m.server == "llama" else "vllm not found: install it or pass --vllm")
    pid = launch(procs, m, args.mode, exe, root, args.port, reason)
    log = procs.log_path(NAME)
    print(f"started {Path(exe).name} for {m.name} (pid {pid}); log {log}; waiting for "
          f"http://{HOST}:{args.port}{ready_path(m.server)}")
    t0 = time.monotonic()
    result = wait_ready(procs, m.server, args.port, args.timeout)
    if result == "ready":
        entry = record_load(procs, time.monotonic() - t0) or {}
        print(f"ready after {entry.get('seconds', time.monotonic() - t0):.1f} s: {m.name} served as "
              f"{m.served_name} on http://{HOST}:{args.port}/v1")
        return 0
    if result == "exited":
        print(f"the server exited; last lines of {log}:", file=sys.stderr)
        for line in log.read_text(errors="replace").splitlines()[-20:]:
            print("  " + line, file=sys.stderr)
    else:
        print(f"still loading after {args.timeout:g} s; it keeps going in the background "
              f"(python -m sts_cua.serve status / stop)", file=sys.stderr)
    return 1


def cmd_stop(args) -> int:
    pids = stop_server(ProcessManager(args.state_dir))
    print(f"stopped {', '.join(map(str, pids))}" if pids else "no model server running")
    return 0


def cmd_status(args) -> int:
    info = status_info(ProcessManager(args.state_dir), args.port, gpu(), served(args.port))
    if args.json:
        print(json.dumps(info))
        return 0
    if not info["running"] and info["served"] is None:
        print("no model server running")
    else:
        print(f"model server: pid {', '.join(map(str, info['pids'])) or '?'}; model {info['model'] or '?'}"
              f" (mode {info['mode'] or '?'}); serves {', '.join(info['served'] or []) or 'nothing yet (loading?)'}"
              f" on port {info['port']}")
    g = info["gpu"]
    print(f"VRAM: {g['used_mib'] / 1024:.1f} GiB used, {g['free_mib'] / 1024:.1f} free of "
          f"{g['total_mib'] / 1024:.1f}" if g else "VRAM: nvidia-smi not readable")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sts_cua.serve", description=__doc__.splitlines()[0])
    parser.add_argument("--models-dir", type=Path, help=f"where llama.cpp models go, as <dir>/<repo>/<file> (default "
                        f"${MODELS_ENV}, else ~/models, else {WINDOWS_MODELS} if only it exists)")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(), help="pid file run/model.pid, log logs/model.log")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="registry models: sizes, budgets, downloaded")
    p = sub.add_parser("download", help="fetch a model's files and check their sizes")
    p.add_argument("model", choices=list(MODELS))
    p = sub.add_parser("start", help="start a model server and wait until it answers")
    p.add_argument("model", choices=[BEST, *MODELS], help="a registry model, or 'best': the strongest downloaded one that fits")
    p.add_argument("--mode", choices=MODES, required=True, help="co-op (13 GiB budget) or solo (20 GiB)")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--force", action="store_true", help="start even if the VRAM check fails")
    p.add_argument("--timeout", type=float, default=START_TIMEOUT, help="seconds to wait for the server")
    p.add_argument("--llama-server", help="llama-server executable (default: $LLAMA_SERVER, ~/llama.cpp/build/bin, PATH)")
    p.add_argument("--vllm", help="vllm executable (default: the venv's, else PATH)")
    sub.add_parser("stop", help="stop the model server")
    p = sub.add_parser("status", help="which model is served, pid, VRAM")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    args.state_dir = args.state_dir.expanduser()
    commands = {"list": cmd_list, "download": cmd_download, "start": cmd_start, "stop": cmd_stop, "status": cmd_status}
    try:
        return commands[args.cmd](args)
    except ServeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
