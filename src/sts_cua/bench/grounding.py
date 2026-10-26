"""Score models on the grounding targets, and live on the click-target page.

Grounding: each model comes from the factory (models/factory.py), so any adapter it builds
works here. The system prompt is prompts/grounding.txt and the goal is "Return only the click
for: <instruction>"; history is empty, so every target is one independent request. When the
model's arguments include a `grounding` flag (an adapter's grounding mode), it is turned on;
when the built model has a `ground(screenshot, instruction)` method, that is called instead
of decide. Local models default to temperature 0.

Per target: hit (the first click lands within the target's radius), error distance in
pixels, wall-clock latency, output tokens, and llama-server's `timings` (generation and
prompt tokens/s) when the reply carries them. A reply with no click is malformed; an
exception (server down, HTTP error) is an error and counts in neither hits nor misses.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import time
from pathlib import Path
from typing import Callable

from PIL import Image

from ..actions import Click
from ..models.base import Decision, Observation
from ..models.factory import build_model
from . import results as R
from .targets import Target

REPO = Path(__file__).resolve().parents[3]
GROUNDING_PROMPT = REPO / "prompts" / "grounding.txt"
CLICK_TARGET_PROMPT = REPO / "prompts" / "click_target.txt"
GOAL = "Return only the click for: {instruction}"
MAX_ERRORS = 3  # stop a model after this many errors in a row (its server is probably down)


def first_click(decision: Decision) -> Click | None:
    return next((a for a in decision.actions if isinstance(a, Click)), None)


def score(target: Target, click: Click | None) -> dict:
    """Hit and error distance for one click (None: malformed)."""
    if click is None:
        return {"hit": False, "error_px": None, "x": None, "y": None, "malformed": True}
    error = math.hypot(click.x - target.x, click.y - target.y)
    return {"hit": error <= target.radius, "error_px": round(error, 1), "x": click.x, "y": click.y,
            "malformed": False}


def _capture(model) -> list[dict]:
    """Keep each raw HTTP response of an adapter with a transport, for llama-server's `timings`."""
    seen: list[dict] = []
    transport = getattr(model, "_transport", None)
    if callable(transport):
        def keep(*args, **kwargs):
            response = transport(*args, **kwargs)
            seen.append(response)
            return response
        model._transport = keep
    return seen


def _timings(decision: Decision | None, seen: list[dict]) -> dict:
    timings = (seen[-1].get("timings") if seen and isinstance(seen[-1], dict) else None) \
        or ((decision.raw or {}).get("timings") if decision is not None else None) or {}
    return {"tokens_per_s": timings.get("predicted_per_second"),
            "prompt_tokens_per_s": timings.get("prompt_per_second")}


def model_args(base: argparse.Namespace, model: str, instruction: str | None = None,
               prompt_file: Path | None = GROUNDING_PROMPT) -> argparse.Namespace:
    args = copy.copy(base)
    args.model = model
    if instruction is not None:
        args.goal = GOAL.format(instruction=instruction)
    if not getattr(args, "prompt_file", None) and prompt_file is not None:
        args.prompt_file = str(prompt_file)
    if getattr(args, "temperature", None) is None:
        args.temperature = 0.0
    if hasattr(args, "grounding"):
        args.grounding = True
    return args


def ground_one(model, target: Target, image: Image.Image, clock: Callable[[], float] = time.monotonic) -> dict:
    seen = _capture(model)
    decision = None
    start = clock()
    try:
        ground = getattr(model, "ground", None)
        if callable(ground):
            decision = ground(image, target.instruction)
        else:
            decision = model.decide(Observation(1, image, []))
    except Exception as exc:  # server down, HTTP error, adapter failure
        return {"id": target.id, "hit": False, "error_px": None, "x": None, "y": None, "malformed": False,
                "latency_s": round(clock() - start, 3), "error": repr(exc)[:300]}
    latency = clock() - start
    item = {"id": target.id, **score(target, first_click(decision)), "latency_s": round(latency, 3),
            "output_tokens": decision.usage.output_tokens, **_timings(decision, seen),
            "reply": (decision.thought or "")[:200], "error": None}
    return item


def summarize(items: list[dict]) -> dict:
    answered = [i for i in items if not i.get("error")]
    hits = sum(1 for i in answered if i["hit"])
    malformed = sum(1 for i in answered if i["malformed"])
    latencies = [i["latency_s"] for i in answered]
    return {
        "targets": len(items),
        "answered": len(answered),
        "errors": len(items) - len(answered),
        "hits": hits,
        "malformed": malformed,
        "hit_rate": round(hits / len(answered), 4) if answered else None,
        "malformed_rate": round(malformed / len(answered), 4) if answered else None,
        "median_error_px": R.median([i["error_px"] for i in answered if i["error_px"] is not None]),
        "latency_p50_s": R.median(latencies),
        "latency_p95_s": R.percentile(latencies, 95),
        "tokens_per_s": R.median([i.get("tokens_per_s") for i in answered]),
        "prompt_tokens_per_s": R.median([i.get("prompt_tokens_per_s") for i in answered]),
    }


def run_model(name: str, targets: list[Target], bench_dir: Path, base_args: argparse.Namespace,
              build: Callable = build_model, clock: Callable[[], float] = time.monotonic,
              progress: Callable[[str], None] | None = None) -> dict:
    items: list[dict] = []
    errors_in_row = 0
    adapter = None
    for n, target in enumerate(targets, 1):
        model = build(model_args(base_args, name, target.instruction))
        adapter = getattr(model, "name", name)
        image = Image.open(Path(bench_dir) / target.image).convert("RGB")
        item = ground_one(model, target, image, clock)
        items.append(item)
        if progress:
            mark = "error" if item["error"] else ("hit" if item["hit"] else ("malformed" if item["malformed"] else "miss"))
            progress(f"{name} {n}/{len(targets)} {target.id}: {mark} {R.fmt(item['error_px'])} px "
                     f"{item['latency_s']:.2f} s")
        errors_in_row = errors_in_row + 1 if item["error"] else 0
        if errors_in_row >= MAX_ERRORS:
            if progress:
                progress(f"{name}: {MAX_ERRORS} errors in a row, skipping the rest ({item['error']})")
            break
    return {"model": name, "adapter": adapter, "summary": summarize(items), "items": items}


def run(models: list[str], targets: list[Target], bench_dir: Path, base_args: argparse.Namespace,
        game_build: str | None = None, before_model: Callable[[str], None] | None = None, **kwargs) -> dict:
    """Every model on every target, one model after another (before_model can start each one's server)."""
    entries = []
    for name in models:
        if before_model:
            before_model(name)
        entries.append(run_model(name, targets, bench_dir, base_args, **kwargs))
    builds = sorted({(t.source or {}).get("game_build") or "unknown" for t in targets})
    return {
        "kind": "grounding",
        "created_at": R.now_iso(),
        "game_build": game_build or ", ".join(builds),
        "targets": len(targets),
        "runs": len({(t.source or {}).get("run") for t in targets}),
        "models": entries,
    }


def markdown(result: dict) -> str:
    lines = [f"# Grounding, {result['created_at']}", "",
             f"{result['targets']} targets from {result['runs']} runs, game build {result['game_build']}.", "",
             "| Model | Hit rate | Median error (px) | Latency p50 / p95 (s) | Tokens/s | Malformed | Errors |",
             "|---|---|---|---|---|---|---|"]
    for m in result["models"]:
        s = m["summary"]
        lines.append(f"| {m['model']} | {R.pct(s['hit_rate'])} ({s['hits']}/{s['answered']}) | "
                     f"{R.fmt(s['median_error_px'])} | {R.fmt(s['latency_p50_s'], 2)} / {R.fmt(s['latency_p95_s'], 2)} | "
                     f"{R.fmt(s['tokens_per_s'])} | {s['malformed']} | {s['errors']} |")
    return "\n".join(lines) + "\n"


# ---- live clicks on tools/click_target.py


def read_score(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"hits": 0, "misses": 0, "button": None, "clicks": []}


def run_clicks(model, screen, score_fn: Callable[[], dict], n: int, settle: float = 0.5,
               clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
               progress: Callable[[str], None] | None = None) -> dict:
    """n steps of: screenshot, decide, perform the first click, read the target page's score."""
    seen = _capture(model)
    items = []
    for step in range(1, n + 1):
        before = score_fn()
        shot = screen.capture()
        start = clock()
        try:
            decision = model.decide(Observation(step, shot, []))
        except Exception as exc:
            items.append({"step": step, "hit": False, "malformed": False, "error_px": None,
                          "latency_s": round(clock() - start, 3), "error": repr(exc)[:300]})
            continue
        latency = clock() - start
        click = first_click(decision)
        item = {"step": step, "hit": False, "malformed": click is None, "error_px": None,
                "latency_s": round(latency, 3), "output_tokens": decision.usage.output_tokens,
                **_timings(decision, seen), "error": None}
        if click is not None:
            screen.perform(click)
            sleep(settle)
            after = score_fn()
            new = after.get("clicks", [])[len(before.get("clicks", [])):]
            item["hit"] = bool(new) and bool(new[-1].get("hit"))
            button = before.get("button")
            if button:
                bx, by, bw, bh = button
                item["error_px"] = round(math.hypot(click.x - (bx + bw / 2), click.y - (by + bh / 2)), 1)
            item.update(x=click.x, y=click.y)
        items.append(item)
        if progress:
            progress(f"click {step}/{n}: {'hit' if item['hit'] else 'malformed' if item['malformed'] else 'miss'} "
                     f"{item['latency_s']:.2f} s")
    return {"summary": summarize([{**i, "id": i["step"]} for i in items]), "items": items}
