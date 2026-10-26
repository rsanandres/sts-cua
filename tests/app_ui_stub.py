"""Development stub for the control app's web page: the static files plus canned JSON.

Implements every endpoint in docs/app-api.md with fake data, so the page can be built
and tested without the real server, the game or a GPU. Not used in production.

    python tests/app_ui_stub.py --port 8765    # then open the printed login URL

Login works like the real server: GET /login?token=<token> sets the cookie.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "src" / "sts_cua" / "app" / "static"
TOKEN = "test-token"
COOKIE = "sts_cua_token"
SAFE = re.compile(r"^[\w.-]+$")
XSS = '<script>window.__xss = 1</script><img src=x onerror="window.__xss = 2">'
LOGS = ("steam", "game", "model", "download", "vllm", "supervisor", "app", "coop", "bench", "install")
BENCH_ACTIONS = ("extract", "review", "delete", "grounding", "baseline", "resources", "clicks", "write-docs")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

NOW = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def png(label: str, color=(40, 60, 90)) -> bytes:
    img = Image.new("RGB", (1280, 720), (24, 26, 32))
    draw = ImageDraw.Draw(img)
    draw.rectangle((80, 80, 1200, 640), outline=color, width=6)
    draw.rectangle((540, 300, 740, 420), fill=color)
    draw.text((100, 100), label, fill=(230, 230, 230))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def combat_state(floor: int, hp: int, enemy: str = "Nibbit") -> dict:
    return {
        "state_type": "monster", "act": 1, "floor": floor, "ascension": 0, "character": "The Ironclad",
        "player": {"hp": hp, "max_hp": 80, "block": 5, "energy": 3, "gold": 99},
        "hand": [{"slot": 1, "name": "Strike", "cost": "1", "target": "AnyEnemy"},
                 {"slot": 2, "name": "Defend", "cost": "1", "target": "Self"}],
        "round": 2,
        "enemies": [{"name": enemy, "hp": 20, "max_hp": 43, "block": 0, "intents": ["Attack 12"]}],
    }


def make_steps(n: int) -> list[dict]:
    steps = []
    for i in range(1, n + 1):
        rec = {
            "step": i, "time": iso(60 - i), "screenshot": f"screenshots/step_{i:04d}.png",
            "thought": f"Step {i}: play Strike on the Nibbit, then end the turn.",
            "reasoning": None, "stop_reason": "end_turn", "done": False,
            "proposed": [{"kind": "click", "x": 640, "y": 600, "button": "left", "count": 1, "modifiers": None}],
            "usage": {"input_tokens": 1500, "output_tokens": 80, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0, "cost_usd": 0.0021},
            "executed": [{"kind": "click", "x": 640, "y": 600, "button": "left", "count": 1, "modifiers": None},
                         {"kind": "key", "keys": "e", "repeat": 1}],
            "state": combat_state(floor=1 + i // 10, hp=80 - i),
            "notes_shown": ["n0001"] if i % 2 else [],
        }
        if i == 3:
            rec["thought"] = "Thinking about " + XSS + " literally."
        if i == 4:
            rec["error"] = "xdotool: click outside the window"
            rec["executed"] = []
        steps.append(rec)
    return steps


def make_runs() -> dict[str, dict]:
    runs = {}
    specs = [
        ("20261025T170000Z", "game", "claude-haiku-5-5", "died", 7, 7.0, 0.31, 64, "run_over:died"),
        ("20261025T165500Z", "reset", "claude-haiku-5-5", None, None, None, 0.02, 4, "done"),
        ("20261025T150000Z", "game", "claude-sonnet-5-5", "abandoned", 17, 34.33, 1.12, 210, "max_cost"),
        ("20261024T210000Z", "game", "qwen3-vl-8b", "unknown", 3, 3.0, 0.0, 120, "stuck"),
        ("20261024T200000Z", "game", "scripted", None, None, None, None, None, None),
    ]
    for k, (run_id, kind, model, outcome, floor, score, cost, steps, end) in enumerate(specs):
        started = iso(60 + k * 300)
        summary = None if end is None else {
            "end_reason": end, "steps": steps, "cost_usd": cost, "seconds": 900.0, "ended_at": iso(k * 300),
            "tokens": {"input": 90000, "output": 4000, "cache_read": 0, "cache_write": 0},
        }
        score_d = None if outcome is None else {
            "outcome": outcome, "score": score, "max_floor": floor, "act": 1, "bosses_killed": 0,
            "character": "The Ironclad", "ascension": 0, "started": True,
            "combats": [{"floor": 1, "act": 1, "room": "monster", "enemies": ["Nibbit"], "first_step": 2,
                         "last_step": 9, "steps": 8, "hp_start": 80, "hp_end": 71, "hp_lost": 9, "result": "won"}],
        }
        runs[run_id] = {
            "row": {"run_id": run_id, "kind": kind, "model": model, "started_at": started, "end_reason": end,
                    "steps": steps, "cost_usd": cost, "outcome": outcome, "floor": floor, "score": score},
            "meta": {"kind": kind, "model": model, "goal": "Play a run of Slay the Spire 2.",
                     "screen_size": [1280, 720], "display": ":99", "started_at": started},
            "summary": summary, "score": score_d,
            "steps": make_steps(12 if k == 0 else 3),
        }
    return runs


def make_notes() -> list[dict]:
    def note(i, kind, text, keys, status, shown, mean=None):
        return {
            "id": f"n{i:04d}", "kind": kind, "text": text, "keys": keys, "status": status, "shown_n": shown,
            "utility": {"shown": {"n": shown, "mean": 0.2, "m2": 0.1}, "hidden": {"n": 3, "mean": 0.3, "m2": 0.1},
                        "mean": mean, "lo": None if mean is None else mean - 0.1, "hi": None if mean is None else mean + 0.1},
            "downvotes": 0, "source_runs": ["20261024T210000Z"], "created": iso(2000), "updated": iso(100),
        }
    return [
        note(1, "enemy", "Nibbits hit hard on turn 2; block first.", {"enemy": "Nibbit", "act": "1"}, "active", 14, 0.12),
        note(2, "ui", "The End Turn button is at the bottom right. <b>Not</b> HTML " + XSS, {"screen": "monster"}, "probation", 2),
        note(3, "card", "Bash before Strike.", {"card": "Bash"}, "retired", 9, -0.05),
        note(4, "pitfall", "Don't click Abandon Run on the main menu.", {"screen": "menu"}, "probation", 0),
    ]



def make_evals() -> dict[str, dict]:
    """One paired experiment, its report built by the real sts_cua.eval.build_report."""
    from sts_cua import eval as evaluation

    arms = ["none", "frozen:memory/frozen/notes-20261025.jsonl"]
    seeds = ["SEEDA1", "SEEDB2", "SEEDC3", "SEEDD4"]
    plan = {"name": "memory-ab1", "created_at": iso(600), "mode": "paired",
            "arms": [{"label": a, "memory": None, "sha256": None} for a in arms], "seeds": seeds,
            "runs_per_arm": 4, "character": "the Ironclad", "ascension": 0, "max_attempts": 2,
            "schedule": evaluation.schedule(arms, seeds, 4)}
    scores = {"none": [7, 12, 9, 20], arms[1]: [11, 15, 9, 37.33]}
    results = []
    for slot in plan["schedule"][:7]:  # one run still to play
        score = scores[slot["arm"]][slot["pair"]]
        results.append({**slot, "attempt": 1, "valid": True, "invalid_reason": None, "warnings": [],
                        "seed_played": slot["seed"], "score": score, "floor": int(score), "outcome": "died",
                        "cost_usd": 0.3, "end_reason": "run_over:died"})
    report = evaluation.build_report(plan, results, stop_reason="budget")
    return {plan["name"]: {"plan": plan, "results": results, "report": report}}


def make_targets() -> list[dict]:
    out = []
    for i in range(1, 31):
        run = "20261025T181542Z" if i <= 20 else "20261025T164617Z"
        text = "Click the End Turn button" if i % 3 == 0 else f"Play card {i % 5 + 1} on the Nibbit"
        if i == 7:
            text = "Click " + XSS
        out.append({"id": f"{run}-{i:04d}", "instruction": text, "state_type": "monster", "check": "card_played"})
    return out


def bench_results() -> dict:
    def g(model, local, hits, p50, tps, **extra):
        return {"model": model, "local": local, "created_at": iso(300), "game_build": "v0.107.1", "targets": 40,
                "hit_rate": hits / 40, "hits": hits, "answered": 40, "errors": 0, "malformed": 1,
                "median_error_px": 14.5, "latency_p50_s": p50, "latency_p95_s": p50 * 1.8, "tokens_per_s": tps,
                "dir": "20261026T100000Z", **extra}

    def fits(coop, solo, need, how="est."):
        return {"coop": {"fits": coop, "need_gib": need, "how": how}, "solo": {"fits": solo, "need_gib": need, "how": how}}

    return {
        "grounding": [g("claude-haiku-5-5", False, 37, 2.1, None), g("holo4-27b", True, 33, 3.4, 41.2),
                      g("ui-venus-2-9b", True, 31, 1.2, 88.0)],
        "clicks": [{"model": "ui-venus-2-9b", "local": True, "created_at": iso(200), "hits": 18, "answered": 20,
                    "malformed": 0, "errors": 0, "latency_p50_s": 1.1, "latency_p95_s": 1.9, "median_error_px": 9.0}],
        "resources": [{"model": "holo4-27b", "mode": "solo", "created_at": iso(250), "vram_used_gib": 22.1,
                       "vram_total_gib": 23.99, "model_vram_gib": 17.4, "budget_gib": 20.0, "fits": True,
                       "server_rss_gib": 1.3, "ram_used_gib": 11.2, "game_rss_gib": 3.6}],
        "ranking": [{"model": "claude-haiku-5-5", "hit_rate": 0.925, "latency_p50_s": 2.1, "local": False,
                     "fits": {"coop": {"fits": None, "need_gib": None, "how": "api"},
                              "solo": {"fits": None, "need_gib": None, "how": "api"}}},
                    {"model": "holo4-27b", "hit_rate": 0.825, "latency_p50_s": 3.4, "local": True,
                     "fits": fits(False, True, 17.4, "measured")},
                    {"model": "ui-venus-2-9b", "hit_rate": 0.775, "latency_p50_s": 1.2, "local": True,
                     "fits": fits(True, True, 11.3)}],
        "recommended": {"coop": "ui-venus-2-9b", "solo": "holo4-27b"},
        "markdown": "Grounding (`bench grounding`): 40 targets from 3 Claude runs.\n\n| Model | Hit rate |\n|---|---|\n"
                    "| holo4-27b | 82% |\n\nRecommended: co-op **ui-venus-2-9b**, solo **holo4-27b**.\n",
    }


class Stub:
    """Mutable fake backend state shared by the handler threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict]] = []
        self.runs = make_runs()
        self.notes = make_notes()
        self.evals = make_evals()
        self.running = True
        self.live_step = 5
        self.busy_polls = 0
        self.vllm = True  # the model server
        self.served = "holo4-27b"
        self.downloading = None
        self.coop = False
        self.images: dict[str, bytes] = {}
        self.targets = make_targets()
        self.bench_polls = 0  # GET /api/bench calls left before the running job finishes
        self.bench_job = None  # the running job's action
        self.bench_last = {
            "action": "grounding", "argv": ["/venv/bin/python", "-m", "sts_cua.bench", "grounding", "holo4-27b",
                                            "--mode", "solo", "--serve"],
            "pid": 707, "started_at": iso(30), "models": ["holo4-27b"], "mode": "solo", "serve": True,
            "running": False, "exit_code": 0,
            "output": [f"holo4-27b {i}/40 20261025T181542Z-{i:04d}: hit {i * 1.5:.1f} px 3.40 s" for i in range(1, 41)]
                      + ["| holo4-27b | 82% (33/40) | 14.5 |", "wrote runs/bench/20261026T100000Z/results.json " + XSS],
        }
        self.llama_found = False
        self.install_polls = 0
        self.install_last = None

    def image(self, key: str) -> bytes:
        if key not in self.images:
            self.images[key] = png(key)
        return self.images[key]

    def status(self) -> dict:
        def proc(on, minutes, pid):
            return {"running": on, "pid": pid if on else None, "since": iso(minutes) if on else None}
        busy = None
        if self.busy_polls > 0:
            self.busy_polls -= 1
            busy = "stack_up"
        return {
            "processes": {"steam": proc(True, 300, 101), "xvfb": proc(True, 301, 99), "game": proc(True, 290, 202),
                          "model": proc(self.vllm, 120, 303), "supervisor": proc(self.running, 45, 404)},
            "gpu": {"used_mib": 17600, "total_mib": 24564},
            "ram": {"used_mib": 21000, "total_mib": 32000},
            "budget": {"date": "2026-10-25", "spent_usd": 0.42, "cap_usd": 2.0},
            "mod": {"reachable": True, "state_type": "monster", "act": 1, "floor": 3, "hp": 61, "max_hp": 80},
            "busy": busy,
            "coop": proc(self.coop, 5, 505),
        }

    def models(self) -> dict:
        from sts_cua.models.registry import MODELS, budget

        have = {"holo4-27b", "ui-venus-2-9b"}
        rows = []
        for m in sorted(MODELS.values(), key=lambda m: -m.rank):
            total = int(m.file_gib * 2**30)
            got = total if m.name in have else (total // 3 if m.name == self.downloading else 0)
            rows.append({"name": m.name, "family": m.family, "server": m.server, "repo": m.repo, "rank": m.rank,
                         "file_gib": round(m.file_gib, 2), "need_gib": round(m.need_gib, 2),
                         "fits": {mode: m.fits(budget(mode)) for mode in ("coop", "solo")},
                         "downloaded": m.name in have,
                         "download": {"bytes": got, "total": total, "running": m.name == self.downloading},
                         "license": m.license, "thinking": m.thinking, "notes": m.notes + " <b>x</b>",
                         "running": self.vllm and m.name == self.served})
        return {"models": rows, "best": {"coop": "ui-venus-2-9b", "solo": "holo4-27b"},
                "gpu": {"used_mib": 17600, "free_mib": 6964, "total_mib": 24564},
                "server": {"running": self.vllm, "pid": 303 if self.vllm else None,
                           "model": self.served if self.vllm else None, "mode": "solo" if self.vllm else None,
                           "served": [self.served] if self.vllm else None, "port": 8000, "ready": self.vllm},
                "download": {"running": bool(self.downloading), "model": self.downloading}}

    def bench(self) -> dict:
        from sts_cua.models.registry import MODELS, budget

        if self.bench_job and self.bench_polls > 0:
            self.bench_polls -= 1
            if self.bench_polls == 0:
                self.finish_bench(0)
        running = bool(self.bench_job)
        have = {"holo4-27b", "ui-venus-2-9b"}
        return {
            "job": {"running": running, "pid": 808 if running else None, "since": iso(0.5) if running else None,
                    "action": self.bench_job, "external": False},
            "last": self.bench_last,
            "targets": {"count": len(self.targets), "rejected": 2, "error": None, "sheets_stale": False,
                        "items": self.targets,
                        "sheets": [{"name": f"sheet-0{i}.png", "url": f"/api/bench/review/sheet-0{i}.png?v=1"} for i in (1, 2)]},
            "models": {"local": [{"name": m.name, "server": m.server, "thinking": m.thinking, "rank": m.rank,
                                  "need_gib": round(m.need_gib, 2), "downloaded": m.name in have,
                                  "fits": {mode: m.fits(budget(mode)) for mode in ("coop", "solo", "max")}}
                                 for m in sorted(MODELS.values(), key=lambda m: -m.rank)],
                       "reference": ["claude-haiku-5-5", "claude-sonnet-5-5"]},
            "modes": ["coop", "solo", "max"],
            "baselines": {"pre-server": {"time": iso(400), "mode": "solo", "gpu_used_mib": 4700, "ram_used_mib": 9100}},
            "server": {"running": self.vllm, "model": self.served if self.vllm else None, "mode": "solo" if self.vllm else None},
            "clicks_display": ":98",
        }

    def start_bench(self, action: str, body: dict) -> dict:
        self.bench_job = action.replace("-", "_")
        self.bench_polls = 2
        self.bench_last = {"action": self.bench_job, "argv": ["/venv/bin/python", "-m", "sts_cua.bench", action],
                           "pid": 808, "started_at": NOW.isoformat(), "models": body.get("models") or [],
                           "mode": body.get("mode"), "serve": bool(body.get("serve")), "running": True,
                           "exit_code": None, "output": [f"--- sts_cua.app started: python -m sts_cua.bench {action}",
                                                         "progress 1/2 " + XSS]}
        return {"started": True, "pid": 808, "action": self.bench_job, "argv": self.bench_last["argv"]}

    def finish_bench(self, code: int) -> None:
        last = self.bench_last
        last.update(running=False, exit_code=code)
        last["output"] = last["output"] + ["done"]
        if last["action"] == "write_docs" and code == 0:
            last["changed"] = {"rows": ["ram_server"], "diff": [
                "-- The vLLM process itself: **measure**",
                "+- The vLLM process itself: 2.1 GiB RSS <!-- bench:ram_server --> " + XSS]}
        if last["action"] == "delete":
            self.targets = self.targets[1:]
        self.bench_job = None

    def llama(self) -> dict:
        running = self.install_polls > 0
        if running:
            self.install_polls -= 1
            if self.install_polls == 0:
                self.install_last["exit_code"] = 1
        tail = [] if not self.install_last else [
            "[install_llama_cpp] nvcc not found. Install the CUDA toolkit for WSL-Ubuntu (cuda-toolkit-12-x only, no driver):",
            "[install_llama_cpp]   https://developer.nvidia.com/cuda-downloads (Linux > x86_64 > WSL-Ubuntu)",
            "[install_llama_cpp] or run this script with --prebuilt, which needs no toolkit. " + XSS]
        last = self.install_last or {}
        return {"found": self.llama_found, "path": "/home/raph/llama.cpp/build/bin/llama-server" if self.llama_found else None,
                "version": "11900 (abc1234)" if self.llama_found else None, "release": None,
                "install": {"running": running, "pid": 909 if running else None, "since": iso(1) if running else None,
                            "prebuilt": last.get("prebuilt"), "exit_code": None if running else last.get("exit_code"),
                            "started_at": last.get("started_at"), "tail": tail, "nvcc_missing": bool(tail)}}

    def live(self) -> dict:
        run_id = next(iter(self.runs))
        if self.running:
            self.live_step += 1
        step = self.live_step
        return {
            "run_id": run_id, "kind": "game", "model": "claude-haiku-5-5", "running": self.running,
            "step": step, "cost_usd": round(0.0021 * step, 4),
            "last": {"step": step, "thought": "Live thought with " + XSS, "time": NOW.isoformat(),
                     "executed": [{"kind": "click", "x": 640, "y": 600, "button": "right", "count": 2, "modifiers": "shift"},
                                  {"kind": "drag", "x0": 100, "y0": 500, "x1": 640, "y1": 300},
                                  {"kind": "type", "text": "<b>hi</b>"}, {"kind": "wait", "seconds": 0.5}],
                     "error": "model: timeout <i>x</i>", "state": combat_state(3, 61), "notes_shown": ["n0001", "n0002"]},
            "screenshot": f"/api/runs/{run_id}/screenshots/step_{step:04d}.png",
        }


def make_handler(stub: Stub, require_auth: bool = True):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_bytes(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authed(self) -> bool:
            if not require_auth:
                return True
            if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                return True
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return COOKIE in cookie and cookie[COOKIE].value == TOKEN

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            if path == "/login":
                if q.get("token") != TOKEN:
                    return self.send_json({"error": "bad token"}, 401)
                self.send_response(302)
                self.send_header("Set-Cookie", f"{COOKIE}={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/")
                self.end_headers()
                return
            if not path.startswith("/api/"):
                name = "index.html" if path == "/" else path.lstrip("/")
                file = STATIC / name
                if SAFE.match(name) and file.is_file():
                    return self.send_bytes(file.read_bytes(), CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
                return self.send_json({"error": "not found"}, 404)
            if path == "/api/health":
                return self.send_json({"ok": True, "version": "0.1.0"})
            if not self.authed():
                return self.send_json({"error": "unauthorized"}, 401)
            with stub.lock:
                return self.route_get(path, q)

        def route_get(self, path, q):
            if path == "/api/status":
                return self.send_json(stub.status())
            if path == "/api/models":
                return self.send_json(stub.models())
            if path == "/api/llama":
                return self.send_json(stub.llama())
            if path == "/api/bench":
                return self.send_json(stub.bench())
            if path == "/api/bench/results":
                return self.send_json(bench_results())
            m = re.match(r"^/api/bench/review/([^/]+)$", path)
            if m:
                if not SAFE.match(m.group(1)) or not re.match(r"^sheet-\d+\.png$", m.group(1)):
                    return self.send_json({"error": "no such sheet"}, 404)
                return self.send_bytes(stub.image("contact " + m.group(1)), "image/png")
            if path == "/api/live":
                return self.send_json(stub.live())
            if path == "/api/screen.png":
                return self.send_bytes(stub.image("fresh capture"), "image/png")
            if path == "/api/ledger":
                return self.send_json({"entries": [r["row"] for r in stub.runs.values()]})
            if path == "/api/runs":
                limit = int(q.get("limit", 50))
                return self.send_json({"runs": [r["row"] for r in stub.runs.values()][:limit]})
            if path == "/api/memory/notes":
                status = q.get("status", "all")
                notes = [n for n in stub.notes if status == "all" or n["status"] == status]
                return self.send_json({"path": "memory/notes.jsonl", "notes": notes})
            if path == "/api/evals":
                return self.send_json({"evals": [{
                    "name": name, "created_at": e["plan"]["created_at"], "mode": e["report"]["mode"],
                    "arms": e["report"]["arms"], "planned_runs": e["report"]["planned_runs"],
                    "valid_runs": e["report"]["valid_runs"], "complete": e["report"]["complete"],
                    "stop_reason": e["report"]["stop_reason"], "cost_usd": e["report"]["cost_usd"],
                    "p_value": (e["report"]["test"] or {}).get("p_value"),
                } for name, e in stub.evals.items()]})
            m = re.match(r"^/api/evals/([^/]+)$", path)
            if m:
                found = stub.evals.get(m.group(1)) if SAFE.match(m.group(1)) else None
                if found is None:
                    return self.send_json({"error": "no such evaluation"}, 404)
                return self.send_json(found)
            m = re.match(r"^/api/logs/([\w.-]+)$", path)
            if m:
                if m.group(1) not in LOGS:
                    return self.send_json({"error": "unknown log"}, 404)
                n = int(q.get("lines", 200))
                lines = [f"{m.group(1)} line {i} <tag>" for i in range(n)]
                return self.send_json({"name": m.group(1), "lines": lines})
            m = re.match(r"^/api/runs/([^/]+)(/steps|/screenshots/([^/]+))?$", path)
            if m and SAFE.match(m.group(1)):
                run = stub.runs.get(m.group(1))
                if m.group(3):
                    if not SAFE.match(m.group(3)) or not m.group(3).endswith(".png"):
                        return self.send_json({"error": "bad name"}, 400)
                    return self.send_bytes(stub.image(f"{m.group(1)} {m.group(3)}"), "image/png")
                if run is None:
                    return self.send_json({"error": "no such run"}, 404)
                if m.group(2) == "/steps":
                    offset, limit = int(q.get("offset", 0)), int(q.get("limit", 50))
                    return self.send_json({"total": len(run["steps"]), "steps": run["steps"][offset:offset + limit]})
                return self.send_json({"meta": run["meta"], "summary": run["summary"], "score": run["score"]})
            return self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if not self.authed():
                return self.send_json({"error": "unauthorized"}, 401)
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self.send_json({"error": "Content-Type must be application/json"}, 415)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self.send_json({"error": "bad json"}, 400)
            with stub.lock:
                stub.calls.append((path, body))
                return self.route_post(path, body)

        def route_post(self, path, body):
            if path == "/api/stack/up":
                stub.busy_polls = 2
                return self.send_json({"started": True})
            if path == "/api/stack/down":
                stub.running = stub.vllm = False
                return self.send_json({"started": True})
            if path == "/api/wsl/shutdown":
                if body.get("confirm") is not True:
                    return self.send_json({"error": "confirm required"}, 400)
                return self.send_json({"shutting_down": True})
            if path == "/api/model/start":
                if stub.vllm:
                    return self.send_json({"error": "a model server is already running; stop it first"}, 409)
                stub.vllm = True
                stub.served = "holo4-27b" if body.get("model") == "best" else body.get("model")
                return self.send_json({"started": True, "pid": 303, "model": stub.served, "mode": body.get("mode"),
                                       "reason": "stub"})
            if path == "/api/model/stop":
                stub.vllm = False
                return self.send_json({"stopped": True, "pids": [303]})
            if path == "/api/model/download":
                if stub.downloading:
                    return self.send_json({"error": "already downloading"}, 409)
                stub.downloading = body.get("model")
                return self.send_json({"started": True, "pid": 606, "model": stub.downloading})
            if path == "/api/llama/install":
                if stub.install_polls > 0:
                    return self.send_json({"error": "llama.cpp is already being installed"}, 409)
                stub.install_polls = 2
                stub.install_last = {"prebuilt": bool(body.get("prebuilt")), "started_at": NOW.isoformat(), "exit_code": None}
                return self.send_json({"started": True, "pid": 909, "argv": ["bash", "tools/install_llama_cpp.sh"]})
            if path == "/api/bench/stop":
                was = bool(stub.bench_job)
                if was:
                    stub.finish_bench(-2)
                return self.send_json({"stopped": True, "pids": [808] if was else []})
            m = re.match(r"^/api/bench/([\w-]+)$", path)
            if m:
                if m.group(1) not in BENCH_ACTIONS:
                    return self.send_json({"error": "no such action"}, 404)
                if stub.bench_job:
                    return self.send_json({"error": f"a bench job is already running ({stub.bench_job})"}, 409)
                if m.group(1) == "grounding" and not body.get("models"):
                    return self.send_json({"error": "models must be a non-empty list"}, 400)
                return self.send_json(stub.start_bench(m.group(1), body))
            if path == "/api/runs/start":
                if stub.running:
                    return self.send_json({"error": "a supervisor is already running"}, 409)
                stub.running = True
                return self.send_json({"started": True, "pid": 4242})
            if path == "/api/runs/stop":
                stub.running = False
                return self.send_json({"stopped": True})
            if path == "/api/coop/start":
                if stub.coop:
                    return self.send_json({"error": "a co-op session is already running"}, 409)
                stub.coop = True
                return self.send_json({"started": True, "pid": 5050})
            if path == "/api/coop/stop":
                was, stub.coop = stub.coop, False
                return self.send_json({"stopped": True, "pids": [5050] if was else []})
            if path == "/api/memory/reflect":
                if not SAFE.match(str(body.get("run_id", ""))):
                    return self.send_json({"error": "bad run_id"}, 400)
                return self.send_json({"started": True})
            m = re.match(r"^/api/memory/notes/([\w.-]+)/retire$", path)
            if m:
                note = next((n for n in stub.notes if n["id"] == m.group(1)), None)
                if note is None:
                    return self.send_json({"error": "no such note"}, 404)
                note["status"] = "retired"
                return self.send_json({"retired": True})
            return self.send_json({"error": "not found"}, 404)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 0, require_auth: bool = True) -> tuple[ThreadingHTTPServer, Stub]:
    """Start the stub in a daemon thread. Returns the server (server.server_port) and its state."""
    stub = Stub()
    server = ThreadingHTTPServer((host, port), make_handler(stub, require_auth))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, stub


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    stub = Stub()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(stub))
    print(f"stub on http://{args.host}:{args.port}/login?token={TOKEN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
