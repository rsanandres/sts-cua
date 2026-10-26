"""The local-model bake-off CLI. See sts_cua.bench for the commands."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from ..models.factory import add_model_args, build_model
from . import grounding, report, resources
from . import results as R
from . import targets as T


def _serve(command: list[str]) -> None:
    """`python -m sts_cua.serve ...` (the serving stream's CLI), if it exists."""
    if importlib.util.find_spec("sts_cua.serve") is None:
        raise SystemExit("--serve needs sts_cua.serve; start the model server by hand instead")
    subprocess.run([sys.executable, "-m", "sts_cua.serve", *command], check=True)


def _wait_ready(base_url: str, timeout: float = 600.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=5):
                return
        except OSError:
            time.sleep(2)
    raise SystemExit(f"model server at {base_url} not ready after {timeout:.0f} s")


def cmd_extract(args) -> None:
    out = T.extract(args.runs_dir, args.bench_dir, max_per_run=args.max_per_run)
    print(json.dumps(out, indent=2))
    print(f"targets in {Path(args.bench_dir) / T.TARGETS_FILE}; check them with `python -m sts_cua.bench review`")


def cmd_review(args) -> None:
    if args.delete:
        n = T.delete(args.delete, args.bench_dir)
        print(f"deleted {n} targets (remembered in {Path(args.bench_dir) / T.REJECTED_FILE})")
    for path in T.contact_sheets(args.bench_dir, columns=args.columns):
        print(path)


def cmd_grounding(args) -> None:
    targets = T.load_targets(args.bench_dir)
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        raise SystemExit(f"no targets in {Path(args.bench_dir) / T.TARGETS_FILE}; run `bench extract` first")
    from ..models import registry

    def before(name: str) -> None:
        if args.serve and name in registry.MODELS:
            _serve(["stop"])  # one model server at a time on port 8000
            _serve(["start", name, "--mode", args.mode])
            _wait_ready(args.base_url)

    result = grounding.run(args.models, targets, Path(args.bench_dir), args, game_build=args.game_build,
                           before_model=before, progress=print)
    out_dir = R.new_dir(args.runs_dir)
    md = grounding.markdown(result)
    R.write(out_dir, result, md)
    print(md)
    print(f"wrote {out_dir / R.RESULTS_FILE}")


def cmd_clicks(args) -> None:
    from ..screen import X11Screen

    out_dir = R.new_dir(args.runs_dir)
    score_file = out_dir / "target.json"
    children = []
    env = {**os.environ, "DISPLAY": args.display}
    try:
        if args.xvfb:
            children.append(subprocess.Popen(["Xvfb", args.display, "-screen", "0", f"{args.size}x24"],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(1.0)
        page = grounding.REPO / "tools" / "click_target.py"
        children.append(subprocess.Popen([sys.executable, str(page), "--out", str(score_file), "--size", args.size,
                                          "--seed", str(args.target_seed)], env=env))
        deadline = time.monotonic() + 10
        while not score_file.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(0.5)
        model = build_model(grounding.model_args(args, args.model_name, prompt_file=grounding.CLICK_TARGET_PROMPT))
        result = grounding.run_clicks(model, X11Screen(args.display), lambda: grounding.read_score(score_file),
                                      args.n, settle=args.settle, progress=print)
    finally:
        for child in reversed(children):
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
    result = {"kind": "clicks", "created_at": R.now_iso(), "model": args.model_name,
              "adapter": getattr(model, "name", args.model_name), **result}
    s = result["summary"]
    md = (f"# Clicks, {args.model_name}, {result['created_at']}\n\n"
          f"{s['hits']}/{s['answered']} hits, {s['malformed']} malformed, {s['errors']} errors, "
          f"latency p50 {R.fmt(s['latency_p50_s'], 2)} s, p95 {R.fmt(s['latency_p95_s'], 2)} s, "
          f"median error {R.fmt(s['median_error_px'])} px from the button centre\n")
    R.write(out_dir, result, md)
    print(md)
    print(f"wrote {out_dir / R.RESULTS_FILE}")


def cmd_baseline(args) -> None:
    snap = resources.snapshot()
    if snap["gpu"] is None:
        print("warning: nvidia-smi gave no reading; the baseline has RAM only", file=sys.stderr)
    path = resources.save_baseline(args.runs_dir, args.label, snap, mode=args.mode)
    print(json.dumps({args.label: snap}, indent=2))
    print(f"wrote {path}")


def cmd_resources(args) -> None:
    baselines = resources.load_baselines(args.runs_dir)
    if "pre-server" not in baselines:
        print("warning: no pre-server baseline (`bench baseline` before starting the server); "
              "model VRAM will be missing", file=sys.stderr)
    served_by = None
    if args.serve:
        _serve(["start", args.model_name, "--mode", args.mode])
        _wait_ready(args.base_url)
        served_by = "sts_cua.serve"
        time.sleep(args.warmup)
    result = resources.measure(args.model_name, args.mode, baselines, game=args.game,
                               server_log=args.server_log, served_by=served_by)
    if not result["server"]["pids"]:
        print("warning: no model server process found (vllm serve / llama-server)", file=sys.stderr)
    if args.game and not result["game"]["running"]:
        print("warning: --game given but SlayTheSpire2 isn't running", file=sys.stderr)
    out_dir = R.new_dir(args.runs_dir)
    md = resources.markdown(result)
    R.write(out_dir, result, md)
    print(md)
    print(f"wrote {out_dir / R.RESULTS_FILE}")


def cmd_report(args) -> None:
    data = report.latest(R.load_all(args.runs_dir))
    print(report.block(data))
    if args.write_docs:
        touched = report.write_docs(args.docs, data)
        print(f"updated {args.docs}: the bench block, and measure rows: {', '.join(touched) or 'none'}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sts_cua.bench", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="grounding targets from Claude runs -> bench/targets.jsonl")
    p.add_argument("--max-per-run", type=int, help="new targets taken from each run at most")

    p = sub.add_parser("review", help="contact sheets of the targets in bench/review/")
    p.add_argument("--delete", nargs="+", metavar="ID", help="drop these targets first (and never re-extract them)")
    p.add_argument("--columns", type=int, default=4)

    p = sub.add_parser("grounding", help="score models on the targets")
    p.add_argument("models", nargs="+", help="--model names the factory builds (local names or Claude IDs)")
    p.add_argument("--limit", type=int, help="first N targets only")
    p.add_argument("--game-build", help="recorded with the results (default: the builds of the targets' runs)")
    p.add_argument("--serve", action="store_true",
                   help="start each local model with `python -m sts_cua.serve start <model> --mode` first")
    p.add_argument("--mode", choices=["coop", "solo", "max"], default="solo", help="with --serve: the VRAM budget to start under")
    add_model_args(p)

    p = sub.add_parser("clicks", help="live clicks on tools/click_target.py (no game needed)")
    p.add_argument("model_name", metavar="model")
    p.add_argument("--display", default=":98", help="X display (default :98, away from the game's :99)")
    p.add_argument("--xvfb", action="store_true", help="start Xvfb on --display for the test")
    p.add_argument("--size", default="1280x720")
    p.add_argument("--n", type=int, default=20, help="clicks")
    p.add_argument("--settle", type=float, default=0.5)
    p.add_argument("--target-seed", type=int, default=0, help="click_target.py's seed, so models see the same spots")
    add_model_args(p)

    p = sub.add_parser("baseline", help="save GPU and RAM use before the model server starts")
    p.add_argument("--label", default="pre-server", choices=["pre-server", "desktop"],
                   help="pre-server: games running, no model server; desktop: before the games too")
    p.add_argument("--mode", choices=["coop", "solo", "max"])

    p = sub.add_parser("resources", help="measure VRAM and RAM with the model server running")
    p.add_argument("model_name", metavar="model")
    p.add_argument("--mode", choices=["coop", "solo", "max"], required=True)
    p.add_argument("--game", action="store_true", help="also record the game, Steam and Xvfb RSS")
    p.add_argument("--server-log", help="vLLM's log: weights and KV cache, for the activations row")
    p.add_argument("--serve", action="store_true", help="start it with `python -m sts_cua.serve start <model>` first")
    p.add_argument("--base-url", default="http://localhost:8000/v1", help="with --serve: wait for this endpoint")
    p.add_argument("--warmup", type=float, default=5.0, help="with --serve: seconds to wait after it answers")

    p = sub.add_parser("report", help="tables and ranking from runs/bench/; --write-docs updates docs/hosting.md")
    p.add_argument("--write-docs", action="store_true")
    p.add_argument("--docs", default="docs/hosting.md")
    for sp in sub.choices.values():
        sp.add_argument("--runs-dir", default="runs")
        sp.add_argument("--bench-dir", default=str(T.BENCH_DIR))
    args = parser.parse_args(argv)
    {"extract": cmd_extract, "review": cmd_review, "grounding": cmd_grounding, "clicks": cmd_clicks,
     "baseline": cmd_baseline, "resources": cmd_resources, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    main()
