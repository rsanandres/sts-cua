"""Turn a run into video material: an MP4 replay with the AI's thoughts as captions, plus a highlight list.

    python -m sts_cua.replay runs/20261025T181043Z                       # replay.mp4 + highlights.md in the run dir
    python -m sts_cua.replay runs/20261025T181043Z --seconds-per-step 2
    python -m sts_cua.replay runs/20261025T181043Z --highlights-only      # no ffmpeg needed

Each step's saved screenshot becomes one frame, held for --seconds-per-step. A bar under the
game shows the step, floor, HP, gold, spend so far and the AI's thought; clicks are circled on
the frame. highlights.md lists fights, floors, big hits, stuck streaks and the end of the run with
their time in replay.mp4 and, when the run was recorded with --record, in screen.mp4.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .actions import action_from_dict, describe
from .loop import screen_diff
from .state import COMBAT_TYPES

BAR_HEIGHT = 150
UNCHANGED = 1.0  # same threshold as LoopConfig.unchanged_threshold
STUCK_MIN = 3  # unchanged steps in a row worth a highlight
BIG_HIT = 10  # HP lost between two steps worth a highlight
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


@dataclass
class Highlight:
    step: int
    kind: str
    text: str


def load_run(run_dir: Path) -> tuple[dict, list[dict], dict]:
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    with (run_dir / "steps.jsonl").open(encoding="utf-8") as f:
        steps = [json.loads(line) for line in f if line.strip()]
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    return meta, steps, summary


def step_diffs(run_dir: Path, steps: list[dict]) -> list[float | None]:
    """Screen change since the previous step: logged by newer runs, recomputed from screenshots for older ones."""
    if any("screen_diff" in r for r in steps):
        return [r.get("screen_diff") for r in steps]
    diffs: list[float | None] = []
    previous = None
    for record in steps:
        shot = Image.open(run_dir / record["screenshot"])
        diffs.append(screen_diff(previous, shot) if previous is not None else None)
        previous = shot
    return diffs


def _acted(record: dict) -> bool:
    return any(a.get("kind") != "wait" for a in record.get("executed") or [])


def _hp(state: dict | None) -> int | None:
    return ((state or {}).get("player") or {}).get("hp")


def _enemies(state: dict | None) -> str:
    names = [e.get("name") for e in (state or {}).get("enemies") or [] if e.get("name")]
    return ", ".join(names) or "enemies"


def highlights(steps: list[dict], diffs: list[float | None], summary: dict) -> list[Highlight]:
    out: list[Highlight] = []
    prev_state: dict | None = None
    in_fight = False
    fight_start_hp = None
    foe = ""
    stuck_from = None
    stuck_len = 0

    def close_stuck() -> None:
        if stuck_from is not None and stuck_len >= STUCK_MIN:
            # The thought behind the first click that did nothing (steps without a thought skipped).
            tried = steps[stuck_from - 1:stuck_from - 1 + stuck_len]
            thought = next((r["thought"].strip() for r in tried if (r.get("thought") or "").strip()), "")
            out.append(Highlight(steps[stuck_from]["step"], "stuck",
                                 f"Screen didn't change for {stuck_len} steps. AI: {thought[:160]}"))

    for i, record in enumerate(steps):
        state = record.get("state")
        stype = (state or {}).get("state_type")
        # A step "didn't change" when the step before it acted and the screen stayed the same.
        if i and _acted(steps[i - 1]) and diffs[i] is not None and diffs[i] < UNCHANGED:
            if stuck_from is None:
                stuck_from, stuck_len = i, 0
            stuck_len += 1
        else:
            close_stuck()
            stuck_from, stuck_len = None, 0

        if state:
            floor, prev_floor = state.get("floor"), (prev_state or {}).get("floor")
            if floor is not None and prev_floor is not None and floor > prev_floor:
                out.append(Highlight(record["step"], "floor", f"Reached floor {floor}"))
            fighting = stype in COMBAT_TYPES and bool(state.get("enemies"))
            if fighting and not in_fight:
                in_fight, fight_start_hp, foe = True, _hp(state), _enemies(state)
                out.append(Highlight(record["step"], "fight", f"Fight starts: {foe} ({stype})"))
            elif in_fight and not fighting and stype != "game_over":
                in_fight = False
                hp, start = _hp(state), fight_start_hp
                lost = f", lost {start - hp} HP" if hp is not None and start is not None else ""
                out.append(Highlight(record["step"], "fight won", f"Beat {foe}{lost} (HP {hp})"))
            hp, prev_hp = _hp(state), _hp(prev_state)
            if hp is not None and prev_hp is not None and prev_hp - hp >= BIG_HIT:
                out.append(Highlight(record["step"], "big hit", f"Took {prev_hp - hp} damage (HP {hp})"))
            if stype == "game_over" and (prev_state or {}).get("state_type") != "game_over":
                out.append(Highlight(record["step"], "run over", f"Run ended on floor {floor} with HP {hp}"))
            prev_state = state
        if record.get("done"):
            out.append(Highlight(record["step"], "done", f"AI says DONE: {(record.get('thought') or '').strip()[:160]}"))
    close_stuck()
    if steps and summary:
        out.append(Highlight(steps[-1]["step"], "end",
                             f"Run log ends: {summary.get('end_reason')}, {summary.get('steps')} steps, "
                             f"${summary.get('cost_usd', 0):.4f}"))
    return out


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def highlights_markdown(run_dir: Path, meta: dict, steps: list[dict], items: list[Highlight],
                        seconds_per_step: float) -> str:
    index = {r["step"]: i for i, r in enumerate(steps)}
    screen = run_dir / "screen.json"
    rec_start = None
    if screen.exists():
        rec_start = datetime.fromisoformat(json.loads(screen.read_text(encoding="utf-8"))["started_at"])
    head = "| replay.mp4 | screen.mp4 | step | what |" if rec_start else "| replay.mp4 | step | what |"
    lines = [
        f"# Highlights: {run_dir.name}",
        "",
        f"Model: {meta.get('model')}. Goal: {meta.get('goal')}",
        "",
        head,
        "|---|---|---|---|" if rec_start else "|---|---|---|",
    ]
    for h in items:
        at = _clock(index.get(h.step, 0) * seconds_per_step)
        text = h.text.replace("|", "/").replace("\n", " ")
        if rec_start:
            when = datetime.fromisoformat(steps[index.get(h.step, 0)]["time"])
            lines.append(f"| {at} | {_clock((when - rec_start).total_seconds())} | {h.step} | **{h.kind}**: {text} |")
        else:
            lines.append(f"| {at} | {h.step} | **{h.kind}**: {text} |")
    return "\n".join(lines) + "\n"


def _font(size: int) -> ImageFont.ImageFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def render_frame(shot: Image.Image, record: dict, spent: float, model: str) -> Image.Image:
    w, h = shot.size
    frame = Image.new("RGB", (w, h + BAR_HEIGHT), (16, 16, 20))
    frame.paste(shot.convert("RGB"), (0, 0))
    draw = ImageDraw.Draw(frame)
    actions = [action_from_dict(a) for a in record.get("executed") or []]
    for a in actions:
        if a.kind == "click":
            draw.ellipse((a.x - 20, a.y - 20, a.x + 20, a.y + 20), outline=(255, 64, 64), width=4)
            draw.ellipse((a.x - 4, a.y - 4, a.x + 4, a.y + 4), fill=(255, 64, 64))
        elif a.kind == "drag":
            draw.line((a.x0, a.y0, a.x1, a.y1), fill=(255, 64, 64), width=4)

    state = record.get("state") or {}
    player = state.get("player") or {}
    status = [f"Step {record['step']}"]
    if state.get("floor") is not None:
        status.append(f"Floor {state['floor']}")
    if player.get("hp") is not None:
        status.append(f"HP {player['hp']}/{player.get('max_hp')}")
    if player.get("gold") is not None:
        status.append(f"Gold {player['gold']}")
    status += [f"${spent:.4f}", model]
    small, body = _font(18), _font(20)
    draw.text((16, h + 10), "  ·  ".join(status), font=small, fill=(150, 200, 255))
    thought = (record.get("thought") or record.get("error") or "").strip().replace("\n", " ")
    y = h + 38
    for line in textwrap.wrap(thought, width=max(40, w // 11))[:3]:
        draw.text((16, y), line, font=body, fill=(240, 240, 240))
        y += 26
    if actions:
        draw.text((16, h + BAR_HEIGHT - 28), "→ " + "; ".join(describe(a) for a in actions)[:150],
                  font=small, fill=(255, 190, 90))
    return frame


def write_replay(run_dir: Path, out: Path, seconds_per_step: float = 1.5, fps: int = 30) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg not found: install it with `sudo apt install ffmpeg` (or use --highlights-only)")
    meta, steps, _ = load_run(run_dir)
    spent = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        for i, record in enumerate(steps):
            spent += (record.get("usage") or {}).get("cost_usd") or 0.0
            shot = Image.open(run_dir / record["screenshot"])
            render_frame(shot, record, spent, str(meta.get("model"))).save(Path(tmp) / f"{i:05d}.png")
        subprocess.run([
            ffmpeg, "-y", "-loglevel", "error", "-framerate", f"1/{seconds_per_step}",
            "-i", str(Path(tmp) / "%05d.png"), "-vf", f"fps={fps},format=yuv420p",
            "-c:v", "libx264", "-crf", "20", str(out),
        ], check=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="sts_cua.replay", description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--seconds-per-step", type=float, default=1.5)
    parser.add_argument("--out", type=Path, help="video path (default: <run_dir>/replay.mp4)")
    parser.add_argument("--highlights-only", action="store_true", help="write highlights.md only")
    args = parser.parse_args(argv)

    meta, steps, summary = load_run(args.run_dir)
    items = highlights(steps, step_diffs(args.run_dir, steps), summary)
    md = highlights_markdown(args.run_dir, meta, steps, items, args.seconds_per_step)
    (args.run_dir / "highlights.md").write_text(md, encoding="utf-8")
    print(md)
    if not args.highlights_only:
        out = args.out or args.run_dir / "replay.mp4"
        write_replay(args.run_dir, out, args.seconds_per_step)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
