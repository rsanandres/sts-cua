"""Read a run directory back into combats and wasted steps, for scoring and reflection.

Uses only what the harness logged: steps.jsonl (actions, errors, the mod's game
state summary), meta.json, summary.json and the per-step screenshots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..loop import LoopConfig, screen_diff


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@dataclass
class Run:
    dir: Path
    meta: dict
    summary: dict
    steps: list[dict]

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def used_memory(self) -> bool:
        return "memory" in self.meta or any("notes_shown" in s for s in self.steps)


def load_run(run_dir: str | Path) -> Run:
    run_dir = Path(run_dir)
    lines = (run_dir / "steps.jsonl").read_text(encoding="utf-8").splitlines()
    steps = [json.loads(line) for line in lines if line.strip()]
    return Run(run_dir, load_json(run_dir / "meta.json"), load_json(run_dir / "summary.json"), steps)


def in_combat(state: dict | None) -> bool:
    return bool(state and state.get("enemies"))


def _hp(state: dict | None) -> tuple[int | None, int | None]:
    player = (state or {}).get("player") or {}
    return player.get("hp"), player.get("max_hp")


@dataclass
class Combat:
    index: int
    steps: list[int]  # step numbers inside the combat
    enemies: list[str]
    act: int | None
    floor: int | None
    hp_start: int | None
    hp_end: int | None
    max_hp: int | None
    result: str  # won, lost, unfinished

    @property
    def hp_lost(self) -> int | None:
        if self.hp_start is None or self.hp_end is None:
            return None
        return max(self.hp_start - self.hp_end, 0)


def combats(steps: list[dict]) -> list[Combat]:
    """Runs of consecutive steps with enemies on screen. Steps with no logged state don't break a run."""
    groups: list[list[dict]] = []
    after: list[dict | None] = []  # first state with player HP after each combat
    current: list[dict] = []
    for step in steps:
        state = step.get("state")
        if state is None:
            continue
        if in_combat(state):
            current.append(step)
            continue
        if current:
            groups.append(current)
            after.append(state)
            current = []
    if current:
        groups.append(current)
        after.append(None)

    out = []
    for i, (group, next_state) in enumerate(zip(groups, after), 1):
        first, last = group[0]["state"], group[-1]["state"]
        hp_start, max_hp = _hp(first)
        end_hp = _hp(next_state)[0] if next_state is not None else None
        hp_end = end_hp if end_hp is not None else _hp(last)[0]
        names = []
        for step in group:
            for enemy in step["state"].get("enemies") or []:
                if enemy.get("name") and enemy["name"] not in names:
                    names.append(enemy["name"])
        if hp_end is not None and hp_end <= 0:
            result = "lost"
        elif next_state is not None:
            result = "won"
        else:
            result = "unfinished"
        out.append(Combat(
            i, [s["step"] for s in group], names, first.get("act"), first.get("floor"),
            hp_start, hp_end, max_hp, result,
        ))
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


def max_floor(steps: list[dict]) -> int | None:
    floors = [s["state"]["floor"] for s in steps if (s.get("state") or {}).get("floor") is not None]
    return max(floors) if floors else None
