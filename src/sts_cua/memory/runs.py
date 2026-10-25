"""Read a run directory back into fights and wasted steps, for scoring and reflection.

Fights come from sts_cua.score.combats (the same per-combat breakdown the supervisor
writes to score.json); this module adds the step numbers and max HP that note scoring
needs, and the wasted-step check, which uses the per-step screenshots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..loop import LoopConfig, screen_diff
from ..score import Combat, combats


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@dataclass
class Run:
    dir: Path
    meta: dict
    summary: dict
    steps: list[dict]  # steps.jsonl records in step order

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def states(self) -> list[dict | None]:
        return [s.get("state") for s in self.steps]

    @property
    def used_memory(self) -> bool:
        return "memory" in self.meta or any("notes_shown" in s for s in self.steps)


def load_run(run_dir: str | Path) -> Run:
    run_dir = Path(run_dir)
    lines = (run_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()
    steps = sorted((json.loads(line) for line in lines if line.strip()), key=lambda s: s["step"])
    return Run(run_dir, load_json(run_dir / "meta.json"), load_json(run_dir / "summary.json"), steps)


@dataclass
class Fight:
    index: int  # 1-based, in run order
    combat: Combat
    steps: list[int]  # step numbers inside the fight
    max_hp: int | None  # at the start of the fight

    @property
    def hp_lost(self) -> int | None:
        """HP lost, never negative (a heal during the fight counts as no loss)."""
        lost = self.combat.hp_lost
        return None if lost is None else max(lost, 0)


def fights(run: Run) -> list[Fight]:
    out = []
    for i, combat in enumerate(combats(run.states), 1):
        records = run.steps[combat.first_step - 1: combat.last_step]
        first = run.steps[combat.first_step - 1].get("state") or {}
        out.append(Fight(i, combat, [s["step"] for s in records], (first.get("player") or {}).get("max_hp")))
    return out


def _acted(step: dict) -> bool:
    return any(a.get("kind") != "wait" for a in step.get("executed") or [])


def wasted_steps(run: Run, threshold: float | None = None) -> dict[int, str]:
    """Step number -> why it was wasted: an error, no action, or actions that left the screen unchanged."""
    threshold = LoopConfig.unchanged_threshold if threshold is None else threshold
    out: dict[int, str] = {}
    steps = run.steps
    for i, step in enumerate(steps):
        if step.get("done"):
            continue
        if step.get("error"):
            out[step["step"]] = "error"
        elif not _acted(step):
            out[step["step"]] = "no action"
        elif i + 1 < len(steps):
            a = run.dir / step.get("screenshot", "")
            b = run.dir / steps[i + 1].get("screenshot", "")
            if a.is_file() and b.is_file():
                with Image.open(a) as img_a, Image.open(b) as img_b:
                    if screen_diff(img_a, img_b) < threshold:
                        out[step["step"]] = "screen unchanged"
    return out
