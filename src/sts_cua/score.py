"""Score a run from its per-step summarized mod states (see state.summarize).

Score is AgenticSTS's (arXiv 2607.02255): 100 for a win, otherwise the floor reached
plus 52/3 per boss killed.

Everything here works on a plain list of summarized states, one per step in order
(None where the mod wasn't reachable), so it runs the same on a live run and on a
finished run's steps.jsonl. Schema facts come from state.py's docstring; inferences
are marked "Assumption". The outcome, floor and seed recorded by the game itself
(sts_cua.history) win over these inferences when the supervisor finds the run there;
score_run stays the per-combat breakdown and the fallback.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .state import COMBAT_TYPES

BOSS_POINTS = 52 / 3
WIN_SCORE = 100.0
# Assumption: Spire 2 Early Access ends after the Act 3 boss(es). The singleplayer state
# doesn't say how many acts a run has. Only a fallback: the supervisor takes the outcome
# from the mod's run history (sts_cua.history) when it finds the run there.
FINAL_ACT = 3
# Menu screens that are overlays (tutorials, popups) and can show up mid-run.
_OVERLAY_MENUS = ("tutorial_prompt", "tutorial", "popup")


@dataclass
class Combat:
    floor: int | None
    act: int | None
    room: str | None  # monster, elite or boss
    enemies: list[str]
    first_step: int  # 1-based step numbers in the run log
    last_step: int
    steps: int
    hp_start: int | None
    hp_end: int | None
    hp_lost: int | None
    result: str  # won, lost, abandoned, unfinished


@dataclass
class RunScore:
    outcome: str  # won, died, abandoned, unknown
    score: float
    max_floor: int | None
    act: int | None
    bosses_killed: int
    character: str | None = None
    ascension: int | None = None
    started: bool = False  # any in-run state was seen
    combats: list[Combat] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def in_run(state: dict | None) -> bool:
    """In-run states carry a floor; menu states never do."""
    return bool(state) and state.get("floor") is not None


def at_main_menu(state: dict | None) -> bool:
    """The main menu with no saved run waiting (no Continue / Abandon Run)."""
    if not state or state.get("state_type") != "menu" or state.get("menu_screen") != "main":
        return False
    options = state.get("menu_options") or []
    return "continue" not in options and "abandon_run" not in options


def _left_run(state: dict | None) -> bool:
    """A menu screen outside a run, as opposed to a popup over one."""
    return bool(state) and state.get("state_type") == "menu" and state.get("menu_screen") not in _OVERLAY_MENUS


def _neutral(state: dict | None) -> bool:
    """No information about the run: mod unreachable, or a popup over the game."""
    return state is None or (state.get("state_type") == "menu" and state.get("menu_screen") in _OVERLAY_MENUS)


def _hp(state: dict | None) -> int | None:
    return ((state or {}).get("player") or {}).get("hp")


def _is_combat(state: dict | None) -> bool:
    return bool(state) and (state.get("state_type") in COMBAT_TYPES or state.get("state_type") == "hand_select")


def combats(states: list[dict | None]) -> list[Combat]:
    """One entry per fight: a run of consecutive combat states on one floor.

    None states and popups inside or right after a fight are skipped.
    """
    out: list[Combat] = []
    i, n = 0, len(states)
    while i < n:
        if not _is_combat(states[i]):
            i += 1
            continue
        floor = states[i].get("floor")
        segment = [i]
        j = i + 1
        while j < n:
            s = states[j]
            if _neutral(s):
                j += 1
                continue
            if not _is_combat(s) or s.get("floor") != floor:
                break
            segment.append(j)
            j += 1
        after = states[j] if j < n else None
        out.append(_combat(states, segment, after))
        i = j
    return out


def _combat(states: list[dict | None], segment: list[int], after: dict | None) -> Combat:
    seen = [states[k] for k in segment]
    first, last = seen[0], seen[-1]
    room = next((s["state_type"] for s in seen if s.get("state_type") in COMBAT_TYPES), None)
    enemies: list[str] = []
    for s in seen:
        for e in s.get("enemies") or []:
            if e.get("name") and e["name"] not in enemies:
                enemies.append(e["name"])

    hp_start = _hp(first)
    hp_end = _hp(after) if in_run(after) and _hp(after) is not None else _hp(last)
    ended = any(s.get("combat_ended") for s in seen)
    after_type = (after or {}).get("state_type")
    if hp_end is not None and hp_end <= 0:
        result = "lost"
    elif ended:
        result = "won"
    elif after is None:
        result = "unfinished"
    elif after_type == "game_over":
        # Assumption (fallback only, see sts_cua.history): the game-over screen also follows
        # a win, and Give Up shows it with HP left. A fight that goes straight to it with HP > 0 counts as won only for a
        # final-act boss; anywhere else it was given up.
        final_boss = room == "boss" and (first.get("act") or 0) >= FINAL_ACT
        result = "won" if final_boss and (hp_end is None or hp_end > 0) else "abandoned"
    elif in_run(after):
        result = "won"
    elif _left_run(after):
        result = "abandoned"
    else:
        result = "unfinished"

    hp_lost = hp_start - hp_end if hp_start is not None and hp_end is not None else None
    return Combat(
        floor=first.get("floor"), act=first.get("act"), room=room, enemies=enemies,
        first_step=segment[0] + 1, last_step=segment[-1] + 1, steps=len(segment),
        hp_start=hp_start, hp_end=hp_end, hp_lost=hp_lost, result=result,
    )


def _final_bosses(states: list[dict | None]) -> int:
    """Bosses the final act's map lists (two on a double-boss map), default 1."""
    for s in reversed(states):
        if in_run(s) and (s.get("act") or 0) >= FINAL_ACT and s.get("bosses"):
            return len(s["bosses"])
    return 1


def points(outcome: str, max_floor: int | None, bosses_killed: int) -> float:
    """The score: 100 for a win, otherwise floor + 52/3 per boss killed."""
    if outcome == "won":
        return WIN_SCORE
    return round((max_floor or 0) + BOSS_POINTS * bosses_killed, 2)


def score_run(states: list[dict | None]) -> RunScore:
    fights = combats(states)
    bosses = [c for c in fights if c.room == "boss" and c.result == "won"]
    final_kills = [c for c in bosses if (c.act or 0) >= FINAL_ACT]
    run_states = [s for s in states if in_run(s)]
    floors = [s["floor"] for s in run_states]
    last = run_states[-1] if run_states else {}

    game_over = [s for s in states if s and s.get("state_type") == "game_over"]
    died = any(c.result == "lost" for c in fights) or any(_hp(s) is not None and _hp(s) <= 0 for s in game_over)
    left = False
    if run_states:
        start = next(i for i, s in enumerate(states) if in_run(s))
        left = any(_left_run(s) for s in states[start:])

    if died:
        outcome = "died"
    elif len(final_kills) >= _final_bosses(states):
        outcome = "won"
    elif game_over or left:
        outcome = "abandoned"
    else:
        outcome = "unknown"  # the log ended mid-run, or no run ever started

    max_floor = max(floors) if floors else None
    return RunScore(
        outcome=outcome, score=points(outcome, max_floor, len(bosses)), max_floor=max_floor, act=last.get("act"), bosses_killed=len(bosses),
        character=last.get("character"), ascension=last.get("ascension"), started=bool(run_states), combats=fights,
    )


def run_over(states: list[dict | None]) -> str | None:
    """Why the run is over, or None while it is still going (or hasn't started).

    Over means a game-over screen, or a menu after the run had started.
    """
    started = False
    for s in states:
        if in_run(s):
            started = True
        if not started:
            continue
        if s and s.get("state_type") == "game_over" or _left_run(s):
            return f"run_over:{score_run(states).outcome}"
    return None


class RunOverDetector:
    """A stop_fn for loop.run: collects each step's state and stops once the run is over."""

    def __init__(self):
        self.states: list[dict | None] = []

    def __call__(self, state: dict | None) -> str | None:
        self.states.append(state)
        return run_over(self.states)


def load_states(run_dir: str | Path) -> list[dict | None]:
    """Each step's summarized state from a run's steps.jsonl, in step order."""
    path = Path(run_dir) / "steps.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r.get("state") for r in sorted(records, key=lambda r: r["step"])]


def run_mode(meta: dict | None) -> str:
    """"coop" for a co-op run (python -m sts_cua --coop, meta mode "coop"), else "solo".

    Co-op runs are not solo data: a teammate shares the fights and the path, so their scores
    don't compare with solo runs. The supervisor plays solo only, and memory scoring and
    reflection skip co-op runs.
    """
    return "coop" if (meta or {}).get("mode") == "coop" else "solo"


def load_meta(run_dir: str | Path) -> dict:
    path = Path(run_dir) / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
