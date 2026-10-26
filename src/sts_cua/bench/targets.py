"""Grounding targets taken from real runs, and a contact sheet to weed out bad ones.

A target is one step of a Claude run where:
- the model (not a co-op rule) made exactly one left click and nothing else but waits,
  and the click ran with no error;
- the next screenshot changed (its screen_diff is at least replay.UNCHANGED);
- the step's thought gives an instruction ("Click the End Turn button", "Play card 2 on
  the Nibbit"), with any coordinates in it removed;
- where the mod's state can say, the click did what the instruction says: ending the turn
  or playing a card changed the hand, energy or enemies, and a map click left the map or
  changed the floor. Steps with no state on either side are kept unchecked.

Steps that pressed a key first (e.g. `2`, then a click on the enemy) are left out: the
screenshot was taken before the key, so it doesn't show the card selected.

bench/targets.jsonl has one JSON object per line: id (<run>-<step>), image (relative to
bench/), instruction, x, y, radius (40 px at 1280x720, scaled with the width), size,
state_type, check (which effect check passed, or null) and source (run, step, model,
thought). Deleted ids go to bench/rejected.txt so `extract` doesn't bring them back.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

from ..replay import UNCHANGED, _font, load_run, step_diffs
from ..state import COMBAT_TYPES

BENCH_DIR = Path("bench")
TARGETS_FILE = "targets.jsonl"
REJECTED_FILE = "rejected.txt"
RADIUS = 40  # px at 1280x720
REFERENCE_WIDTH = 1280
MAX_WORDS = 16

# Verb forms Claude writes in a thought -> the imperative used in the instruction.
_VERBS = {
    "click": "Click", "clicks": "Click", "clicking": "Click", "clicked": "Click",
    "select": "Select", "selects": "Select", "selecting": "Select",
    "choose": "Choose", "chooses": "Choose", "choosing": "Choose",
    "pick": "Pick", "picks": "Pick", "picking": "Pick",
    "play": "Play", "plays": "Play", "playing": "Play",
    "take": "Take", "takes": "Take", "taking": "Take",
    "open": "Open", "opens": "Open", "opening": "Open",
    "confirm": "Confirm", "confirms": "Confirm", "confirming": "Confirm",
    "tap": "Tap",
}
_VERB = re.compile(r"\b(" + "|".join(sorted(_VERBS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
_END_TURN = re.compile(r"\bend(?:ing|s)?\s+(?:my\s+|the\s+|this\s+)?turn\b", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")
# Coordinates must never reach the model being tested: "(640, 300)", "at x 640, y 300", "x=640".
_COORDS = (
    re.compile(r"\s*\b(?:at|near|around)?\s*\(\s*\d+(?:\.\d+)?\s*,\s*\d+(?:\.\d+)?\s*\)", re.IGNORECASE),
    re.compile(r"\s*\b(?:at|near|around)?\s*x\s*[=:]?\s*\d+\s*,?\s*y\s*[=:]?\s*\d+", re.IGNORECASE),
)
# Where a reason starts after the action: "Click Proceed to continue", "..., since ...".
_REASON = re.compile(
    r"\s*(?:[,;:]|\s-\s|—|\bbecause\b|\bsince\b|\bso that\b|\bso\b|\bwhich\b|\bin order to\b|\bthen\b|\band then\b"
    r"|\bto (?!the\b|a\b|an\b|my\b|your\b|its\b|their\b|this\b|that\b)[a-z]+)",
    re.IGNORECASE,
)


@dataclass
class Target:
    id: str
    image: str  # relative to the bench dir
    instruction: str
    x: int
    y: int
    radius: float
    size: tuple[int, int]
    state_type: str | None = None
    check: str | None = None
    source: dict | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "image": self.image, "instruction": self.instruction, "x": self.x, "y": self.y,
                "radius": self.radius, "size": list(self.size), "state_type": self.state_type, "check": self.check,
                "source": self.source}

    @classmethod
    def from_dict(cls, d: dict) -> "Target":
        return cls(id=d["id"], image=d["image"], instruction=d["instruction"], x=int(d["x"]), y=int(d["y"]),
                   radius=float(d.get("radius") or RADIUS), size=tuple(d.get("size") or (1280, 720)),
                   state_type=d.get("state_type"), check=d.get("check"), source=d.get("source"))


def scaled_radius(width: int, radius: float = RADIUS) -> float:
    return round(radius * width / REFERENCE_WIDTH, 1)


def instruction_from(thought: str, state: dict | None = None) -> str | None:
    """A one-line click instruction from a step's thought, or None when it doesn't name one."""
    thought = " ".join((thought or "").split())
    if not thought:
        return None
    stype = (state or {}).get("state_type")
    sentences = [s for s in _SENTENCES.split(thought) if s]
    # The action is usually stated last; the first sentences describe the screen.
    for sentence in reversed(sentences):
        if _END_TURN.search(sentence) and (stype is None or stype in COMBAT_TYPES):
            return "Click the End Turn button"
        match = _VERB.search(sentence)
        if not match:
            continue
        rest = sentence[match.end():]
        for pattern in _COORDS:
            rest = pattern.sub("", rest)
        reason = _REASON.search(rest)
        if reason:
            rest = rest[:reason.start()]
        rest = rest.strip(" .!?\"'")
        if not rest:
            continue
        text = f"{_VERBS[match.group(1).lower()]} {rest}"
        words = text.split()
        if len(words) < 2 or len(words) > MAX_WORDS or re.search(r"\d{3,}", text):
            continue
        return text
    return None


def _changed(a: dict, b: dict, keys: tuple[str, ...]) -> bool:
    return any(a.get(k) != b.get(k) for k in keys)


def effect_check(instruction: str, state: dict | None, after: dict | None) -> tuple[str | None, bool]:
    """(which check applies, whether it passed). (None, True) when nothing can be checked."""
    if not state or not after:
        return None, True
    stype = state.get("state_type")
    combat = ("hand", "player", "enemies", "round", "state_type")
    if instruction == "Click the End Turn button":
        return "end_turn", _changed(state, after, combat)
    if instruction.startswith("Play ") and stype in COMBAT_TYPES:
        return "card_played", _changed(state, after, combat)
    if stype == "map":
        return "map_moved", after.get("state_type") != "map" or after.get("floor") != state.get("floor")
    return None, True


def _single_click(record: dict) -> dict | None:
    acts = [a for a in record.get("executed") or [] if a.get("kind") != "wait"]
    if len(acts) != 1:
        return None
    a = acts[0]
    if a.get("kind") != "click" or a.get("button", "left") != "left" or a.get("count", 1) != 1 or a.get("modifiers"):
        return None
    return a


def is_claude_run(meta: dict) -> bool:
    return str(meta.get("model") or "").startswith("claude")


def candidates(run_dir: Path, skipped: dict[str, int] | None = None) -> list[Target]:
    """Targets in one run (images not copied yet: image is the run's own screenshot path)."""
    skipped = skipped if skipped is not None else {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    meta, steps, _ = load_run(run_dir)
    if not is_claude_run(meta):
        return []
    diffs = step_diffs(run_dir, steps)
    out = []
    for i, record in enumerate(steps):
        if record.get("source", "model") != "model" or record.get("error") or record.get("thought") is None:
            continue
        click = _single_click(record)
        if click is None:
            continue
        if i + 1 >= len(steps):
            skip("last step")
            continue
        diff = diffs[i + 1]
        if diff is None or diff < UNCHANGED:
            skip("screen unchanged")
            continue
        state = record.get("state")
        instruction = instruction_from(record.get("thought") or "", state)
        if not instruction:
            skip("no instruction in thought")
            continue
        check, ok = effect_check(instruction, state, steps[i + 1].get("state"))
        if not ok:
            skip("no effect in mod state")
            continue
        size = tuple(meta.get("screen_size") or Image.open(run_dir / record["screenshot"]).size)
        out.append(Target(
            id=f"{run_dir.name}-{record['step']:04d}", image=record["screenshot"], instruction=instruction,
            x=int(click["x"]), y=int(click["y"]), radius=scaled_radius(size[0]), size=size,
            state_type=(state or {}).get("state_type"), check=check,
            source={"run": run_dir.name, "step": record["step"], "model": meta.get("model"),
                    "game_build": meta.get("game_build"), "thought": record.get("thought")},
        ))
    return out


def load_targets(bench_dir: Path = BENCH_DIR) -> list[Target]:
    path = Path(bench_dir) / TARGETS_FILE
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [Target.from_dict(json.loads(line)) for line in f if line.strip()]


def save_targets(targets: list[Target], bench_dir: Path = BENCH_DIR) -> Path:
    path = Path(bench_dir) / TARGETS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(t.to_dict()) + "\n" for t in targets), encoding="utf-8")
    return path


def rejected(bench_dir: Path = BENCH_DIR) -> set[str]:
    path = Path(bench_dir) / REJECTED_FILE
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def extract(runs_dir: Path = Path("runs"), bench_dir: Path = BENCH_DIR, max_per_run: int | None = None) -> dict:
    """Add new targets from every Claude run under runs_dir. Existing and rejected ids are kept as they are."""
    runs_dir, bench_dir = Path(runs_dir), Path(bench_dir)
    existing = load_targets(bench_dir)
    known = {t.id for t in existing} | rejected(bench_dir)
    skipped: dict[str, int] = {}
    added: list[Target] = []
    runs = 0
    for steps_file in sorted(runs_dir.glob("*/steps.jsonl")):
        run_dir = steps_file.parent
        if not (run_dir / "meta.json").exists():
            continue
        found = [t for t in candidates(run_dir, skipped) if t.id not in known]
        if found:
            runs += 1
        for t in found[:max_per_run]:
            image = Path("images") / f"{t.id}.png"
            (bench_dir / "images").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(run_dir / t.image, bench_dir / image)
            t.image = image.as_posix()
            added.append(t)
    save_targets(existing + added, bench_dir)
    return {"added": len(added), "total": len(existing) + len(added), "runs": runs, "skipped": skipped}


def delete(ids: list[str], bench_dir: Path = BENCH_DIR) -> int:
    """Drop targets by id, remember them in rejected.txt and remove their images."""
    bench_dir = Path(bench_dir)
    drop = set(ids)
    targets = load_targets(bench_dir)
    keep = [t for t in targets if t.id not in drop]
    for t in targets:
        if t.id in drop:
            (bench_dir / t.image).unlink(missing_ok=True)
    save_targets(keep, bench_dir)
    with (bench_dir / REJECTED_FILE).open("a", encoding="utf-8") as f:
        for i in sorted(drop - rejected(bench_dir)):
            f.write(i + "\n")
    return len(targets) - len(keep)


def contact_sheets(bench_dir: Path = BENCH_DIR, out_dir: Path | None = None, columns: int = 4,
                   thumb_width: int = 400, per_sheet: int = 24) -> list[Path]:
    """PNG grids of the targets, each with its circle drawn and its id and instruction underneath."""
    bench_dir = Path(bench_dir)
    out_dir = Path(out_dir) if out_dir else bench_dir / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("sheet-*.png"):
        old.unlink()
    targets = load_targets(bench_dir)
    font = _font(14)
    label_h = 58
    paths = []
    for page in range(math.ceil(len(targets) / per_sheet)):
        chunk = targets[page * per_sheet:(page + 1) * per_sheet]
        thumb_h = round(thumb_width * 9 / 16)
        rows = math.ceil(len(chunk) / columns)
        sheet = Image.new("RGB", (columns * thumb_width, rows * (thumb_h + label_h)), (16, 16, 20))
        draw = ImageDraw.Draw(sheet)
        for k, t in enumerate(chunk):
            col, row = k % columns, k // columns
            ox, oy = col * thumb_width, row * (thumb_h + label_h)
            try:
                shot = Image.open(bench_dir / t.image).convert("RGB")
            except OSError:
                shot = Image.new("RGB", t.size, (60, 0, 0))
            scale = thumb_width / shot.width
            sheet.paste(shot.resize((thumb_width, round(shot.height * scale))).crop((0, 0, thumb_width, thumb_h)),
                        (ox, oy))
            cx, cy, r = ox + t.x * scale, oy + t.y * scale, t.radius * scale
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 64, 64), width=3)
            draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=(255, 64, 64))
            lines = [t.id] + textwrap.wrap(t.instruction, width=thumb_width // 8)[:2]
            for n, line in enumerate(lines):
                draw.text((ox + 6, oy + thumb_h + 4 + n * 17), line, font=font,
                          fill=(150, 200, 255) if n == 0 else (240, 240, 240))
        path = out_dir / f"sheet-{page + 1:02d}.png"
        sheet.save(path)
        paths.append(path)
    return paths
