"""Replay a run's logged states through the loop's stall rule and print the longest streaks.

    .venv/bin/python tools/stall_check.py runs/20261025T194812Z [--limit 15]

A streak counts acting steps in a row whose game state was exactly the same as the step
before (the rule behind LoopConfig.max_stalled). Use it to check a limit against real runs.
"""

import argparse
import json
from pathlib import Path

EXEMPT = ("menu", "game_over")  # LoopConfig.stall_exempt


def streaks(steps: list[dict]) -> list[tuple[int, int, int]]:
    """(length, first step, last step) of each run of stalled acting steps."""
    out, count, start, prev, acted = [], 0, None, None, False
    for r in steps:
        state = r.get("state")
        if state is None:
            continue
        sig = json.dumps(state, sort_keys=True)
        if sig != prev:
            if count:
                out.append((count, start, last))
            count = 0
        elif acted and not r.get("waiting") and state.get("state_type") not in EXEMPT:
            count += 1
            start = r["step"] if count == 1 else start
            last = r["step"]
        prev = sig
        acted = any(a.get("kind") != "wait" for a in r.get("executed") or [])
    if count:
        out.append((count, start, last))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()
    with (args.run_dir / "steps.jsonl").open(encoding="utf-8") as f:
        steps = [json.loads(line) for line in f if line.strip()]
    found = sorted(streaks(steps), reverse=True)
    for length, first, last in found[:10]:
        state = next(r["state"] for r in steps if r["step"] == last)
        flag = "  <- would stop here" if length >= args.limit else ""
        print(f"{length:3d} steps  {first}-{last}  {state.get('state_type')} floor {state.get('floor')}{flag}")
    print(f"{sum(1 for s in found if s[0] >= args.limit)} streak(s) reach --limit {args.limit}")


if __name__ == "__main__":
    main()
