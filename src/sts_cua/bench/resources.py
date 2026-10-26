"""VRAM and RAM with the model server running, against a baseline taken before it started.

    bench baseline --label desktop     # optional: before starting the games
    bench baseline                     # label "pre-server": games running, model server not started
    python -m sts_cua.serve start ...  # or vllm serve / llama-server by hand
    bench resources <model> --mode coop --game

Read with nvidia-smi (whole-GPU used and total, which under WSL includes the Windows
desktop and Raph's Windows copy) and /proc (WSL RAM from /proc/meminfo, process RSS from
/proc/<pid>/status, children included). Per-process GPU memory is usually "[N/A]" under
WSL, so the model's VRAM is the used total minus the pre-server baseline. With a
"desktop" baseline too, the games' VRAM is the pre-server baseline minus the desktop one.

With --server-log, vLLM's startup log gives the weights ("Model loading took") and the KV
cache ("Available KV cache memory"); the rest of the server's VRAM is activations, CUDA
graphs and the vision encoder.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from . import results as R

MIB_PER_GIB = 1024
BASELINE_FILE = "baseline.json"
SERVER_PATTERN = "vllm serve|vllm.entrypoints|llama-server"
GAME_PROCS = {  # name: pgrep arguments
    "game": ["-x", "SlayTheSpire2"],
    "steam": ["-x", "steam"],
    "xvfb": ["-f", "Xvfb :"],
}

Runner = Callable[[list[str]], str]  # argv -> stdout ("" when the command fails)


def run_cmd(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _number(text: str) -> float | None:
    text = text.strip().split()[0] if text.strip() else ""
    try:
        return float(text)
    except ValueError:
        return None  # "[N/A]", "[Not Supported]"


def parse_gpu(text: str) -> list[dict]:
    """`nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv,noheader,nounits`."""
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        used, total = _number(parts[-2]), _number(parts[-1])
        if used is None or total is None:
            continue
        gpus.append({"index": int(_number(parts[0]) or 0), "name": ", ".join(parts[1:-2]),
                     "used_mib": used, "total_mib": total})
    return gpus


def parse_apps(text: str) -> dict[int, float]:
    """`nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits`, pids with a number."""
    out = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and (mib := _number(parts[1])) is not None:
            out[int(parts[0])] = mib
    return out


def parse_meminfo(text: str) -> dict:
    kib = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        value = _number(rest)
        if value is not None:
            kib[name.strip()] = value
    total, available = kib.get("MemTotal"), kib.get("MemAvailable")
    return {
        "total_mib": round(total / 1024, 1) if total else None,
        "used_mib": round((total - available) / 1024, 1) if total and available is not None else None,
        "cached_mib": round(kib["Cached"] / 1024, 1) if "Cached" in kib else None,
        "swap_used_mib": round((kib["SwapTotal"] - kib.get("SwapFree", 0)) / 1024, 1) if "SwapTotal" in kib else None,
    }


def parse_vllm_log(text: str) -> dict | None:
    loading = re.findall(r"Model loading took\s+([\d.]+)\s*GiB", text)
    kv = re.findall(r"Available KV cache memory:\s*([\d.]+)\s*GiB", text)
    if not loading and not kv:
        return None
    return {"weights_gib": float(loading[-1]) if loading else None, "kv_cache_gib": float(kv[-1]) if kv else None}


class Proc:
    """/proc readers, rooted somewhere else in tests."""

    def __init__(self, root: str | Path = "/proc", runner: Runner = run_cmd):
        self.root = Path(root)
        self.runner = runner

    def read(self, *parts: str) -> str:
        try:
            return self.root.joinpath(*parts).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def rss_mib(self, pid: int) -> float:
        match = re.search(r"^VmRSS:\s*(\d+)\s*kB", self.read(str(pid), "status"), re.MULTILINE)
        return int(match.group(1)) / 1024 if match else 0.0

    def children(self) -> dict[int, list[int]]:
        tree: dict[int, list[int]] = {}
        for d in self.root.glob("[0-9]*"):
            stat = self.read(d.name, "stat")
            if not stat:
                continue
            fields = stat[stat.rfind(")") + 2:].split()
            if len(fields) > 1 and fields[1].isdigit():
                tree.setdefault(int(fields[1]), []).append(int(d.name))
        return tree

    def with_descendants(self, pids: list[int]) -> list[int]:
        tree, out, todo = self.children(), [], list(pids)
        while todo:
            pid = todo.pop()
            if pid in out:
                continue
            out.append(pid)
            todo.extend(tree.get(pid, []))
        return sorted(out)

    def cmdline(self, pid: int) -> str:
        return self.read(str(pid), "cmdline").replace("\0", " ").strip()

    def pgrep(self, args: list[str]) -> list[int]:
        return [int(p) for p in self.runner(["pgrep", *args]).split() if p.isdigit()]

    def group(self, args: list[str]) -> dict:
        roots = self.pgrep(args)
        pids = self.with_descendants(roots)
        return {"pids": pids, "rss_mib": round(sum(self.rss_mib(p) for p in pids), 1),
                "cmd": self.cmdline(roots[0])[:200] if roots else None}


def gpu_snapshot(runner: Runner = run_cmd) -> dict | None:
    gpus = parse_gpu(runner(["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total",
                             "--format=csv,noheader,nounits"]))
    if not gpus:
        return None
    apps = parse_apps(runner(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]))
    return {**gpus[0], "gpus": len(gpus), "apps_mib": {str(k): v for k, v in apps.items()}}


def snapshot(proc: Proc | None = None) -> dict:
    proc = proc or Proc()
    return {"time": R.now_iso(), "gpu": gpu_snapshot(proc.runner), "ram": parse_meminfo(proc.read("meminfo"))}


def baseline_path(runs_dir: str | Path = "runs") -> Path:
    return R.bench_runs(runs_dir) / BASELINE_FILE


def load_baselines(runs_dir: str | Path = "runs") -> dict:
    path = baseline_path(runs_dir)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_baseline(runs_dir: str | Path, label: str, snap: dict, mode: str | None = None) -> Path:
    data = load_baselines(runs_dir)
    data[label] = {**snap, "mode": mode}
    path = baseline_path(runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _minus(a: float | None, b: float | None) -> float | None:
    return a - b if a is not None and b is not None else None


def _gib(mib: float | None) -> float | None:
    return round(mib / MIB_PER_GIB, 2) if mib is not None else None


def measure(model: str, mode: str, baselines: dict, proc: Proc | None = None, game: bool = False,
            server_log: str | None = None, served_by: str | None = None) -> dict:
    """One resources result: GPU and RAM now, the model server's RSS, and the deltas against the baselines."""
    proc = proc or Proc()
    snap = snapshot(proc)
    gpu = snap["gpu"] or {}
    pre = (baselines.get("pre-server") or {}).get("gpu") or {}
    desktop = (baselines.get("desktop") or {}).get("gpu") or {}
    server = proc.group(["-f", SERVER_PATTERN])
    pre_ram = (baselines.get("pre-server") or {}).get("ram") or {}
    model_vram = _minus(gpu.get("used_mib"), pre.get("used_mib"))
    games_vram = _minus(pre.get("used_mib"), desktop.get("used_mib"))
    result = {
        "kind": "resources",
        "created_at": snap["time"],
        "model": model,
        "mode": mode,
        "served_by": served_by,
        "gpu": snap["gpu"],
        "ram": snap["ram"],
        "baselines": {k: {"time": v.get("time"), "gpu_used_mib": (v.get("gpu") or {}).get("used_mib"),
                          "ram_used_mib": (v.get("ram") or {}).get("used_mib")} for k, v in baselines.items()},
        "vram_used_gib": _gib(gpu.get("used_mib")),
        "vram_total_gib": _gib(gpu.get("total_mib")),
        "vram_free_gib": _gib(_minus(gpu.get("total_mib"), gpu.get("used_mib"))),
        "model_vram_gib": _gib(model_vram),
        "games_vram_gib": _gib(games_vram),
        "server": {**server, "rss_gib": _gib(server["rss_mib"]) if server["pids"] else None},
        "ram_used_gib": _gib(snap["ram"].get("used_mib")),
        "ram_delta_gib": _gib(_minus(snap["ram"].get("used_mib"), pre_ram.get("used_mib"))),
        "game": None,
    }
    if game:
        groups = {name: proc.group(args) for name, args in GAME_PROCS.items()}
        total = sum(g["rss_mib"] for g in groups.values())
        result["game"] = {**{k: {"pids": g["pids"], "rss_gib": _gib(g["rss_mib"])} for k, g in groups.items()},
                          "running": bool(groups["game"]["pids"]), "rss_gib": _gib(total)}
    if server_log:
        try:
            parsed = parse_vllm_log(Path(server_log).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            parsed = None
        result["server_log"] = parsed
        if parsed and parsed.get("weights_gib") is not None and parsed.get("kv_cache_gib") is not None \
                and result["model_vram_gib"] is not None:
            result["activations_gib"] = round(result["model_vram_gib"] - parsed["weights_gib"] - parsed["kv_cache_gib"], 2)
    return result


def markdown(result: dict) -> str:
    g = result.get("game") or {}
    lines = [
        f"# Resources, {result['model']} ({result['mode']}), {result['created_at']}", "",
        "| Measure | Value |", "|---|---|",
        f"| GPU used / total (GiB) | {R.fmt(result['vram_used_gib'], 2)} / {R.fmt(result['vram_total_gib'], 2)} |",
        f"| Model VRAM, used minus pre-server baseline (GiB) | {R.fmt(result['model_vram_gib'], 2)} |",
        f"| Games VRAM, pre-server minus desktop baseline (GiB) | {R.fmt(result['games_vram_gib'], 2)} |",
        f"| Model server RSS (GiB) | {R.fmt(result['server']['rss_gib'], 2)} |",
        f"| WSL RAM used (GiB) | {R.fmt(result['ram_used_gib'], 2)} |",
    ]
    if g:
        lines.append(f"| Game + Steam + Xvfb RSS (GiB) | {R.fmt(g.get('rss_gib'), 2)} |")
    if result.get("activations_gib") is not None:
        lines.append(f"| vLLM activations etc. (GiB) | {R.fmt(result['activations_gib'], 2)} |")
    return "\n".join(lines) + "\n"
