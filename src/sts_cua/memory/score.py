"""Update note utilities from one run's dense outcomes, then promote or retire notes.

Units and losses (lower is better, both in 0..1):
- enemy, card, strategy notes: each won or lost combat (sts_cua.score.combats) in which the note applied on at
  least one step. Loss = HP lost in that combat / max HP at its start.
- ui, pitfall, event notes: each visit to a screen the note applied to (a stretch of
  consecutive steps where it applied). Loss = share of the visit's steps that were
  wasted (error, no action, or actions that left the screen unchanged; runs.py).

A unit counts as "shown" if the note was shown on any of its steps, else "hidden".
Utility mean = mean hidden loss - mean shown loss, so > 0 means the note helped.
Its interval is mean +/- Z * standard error, the Welch form
sqrt(sd_shown^2 / n_shown + sd_hidden^2 / n_hidden). A group with fewer than two
units uses PRIOR_SD, and no group's sd goes below MIN_SD, so a few lucky combats
can't produce a tight interval.

Gate: once a note has MIN_SHOWN shown and MIN_HIDDEN hidden units, a probation note
becomes active when lo > 0, and any note is retired when hi < 0.
Only notes created before the run started are scored, a run is scored once
(memory_score.json in the run directory marks it), and reset and co-op runs are skipped.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

from ..score import run_mode
from .retrieve import context_from_state, matches
from .runs import Run, fights, wasted_steps
from .store import Group, Note, NoteStore, Utility, now

Z = 1.64  # about a 90% two-sided interval
PRIOR_SD = 0.15
MIN_SD = 0.05
MIN_SHOWN = 5
MIN_HIDDEN = 3
COMBAT_KINDS = ("enemy", "card", "strategy")
SCORE_FILE = "memory_score.json"


def _sd(group: Group) -> float:
    if group.n < 2:
        return PRIOR_SD
    return max(math.sqrt(group.m2 / (group.n - 1)), MIN_SD)


def estimate(utility: Utility) -> None:
    s, h = utility.shown, utility.hidden
    if s.n == 0 or h.n == 0:
        utility.mean = utility.lo = utility.hi = None
        return
    mean = h.mean - s.mean
    se = math.sqrt(_sd(s) ** 2 / s.n + _sd(h) ** 2 / h.n)
    utility.mean, utility.lo, utility.hi = round(mean, 4), round(mean - Z * se, 4), round(mean + Z * se, 4)


def gate(note: Note) -> str | None:
    """The status the note should move to, or None to keep it."""
    u = note.utility
    if note.status == "retired" or u.lo is None or u.shown.n < MIN_SHOWN or u.hidden.n < MIN_HIDDEN:
        return None
    if u.hi < 0:
        return "retired"
    if note.status == "probation" and u.lo > 0:
        return "active"
    return None


def _started(run: Run) -> datetime | None:
    stamp = run.meta.get("started_at")
    return datetime.fromisoformat(stamp) if stamp else None


def observations(note: Note, run: Run, applies: dict[int, bool], wasted: dict[int, str]) -> list[tuple[str, float]]:
    """(shown|hidden, loss) for each unit of this run in which the note applied."""
    shown_at = {s["step"] for s in run.steps if note.id in (s.get("notes_shown") or [])}
    out = []
    if note.kind in COMBAT_KINDS:
        for fight in fights(run):
            if fight.combat.result not in ("won", "lost") or not fight.max_hp or fight.hp_lost is None:
                continue  # unfinished or abandoned fights say little about the note
            if any(applies.get(n) for n in fight.steps):
                arm = "shown" if shown_at.intersection(fight.steps) else "hidden"
                out.append((arm, fight.hp_lost / fight.max_hp))
        return out
    visit: list[int] = []
    for step in run.steps + [None]:
        if step is not None and applies.get(step["step"]):
            visit.append(step["step"])
            continue
        if visit:
            arm = "shown" if shown_at.intersection(visit) else "hidden"
            out.append((arm, sum(n in wasted for n in visit) / len(visit)))
            visit = []
    return out


def score_run(store: NoteStore, run: Run, force: bool = False) -> dict:
    """Update the store in memory (caller saves) and write memory_score.json. Returns the report."""
    marker = run.dir / SCORE_FILE
    if marker.exists() and not force:
        return {"skipped": "already scored", **json.loads(marker.read_text(encoding="utf-8"))}
    if run.meta.get("kind") == "reset":
        return {"skipped": "reset run"}
    if run_mode(run.meta) == "coop":
        return {"skipped": "co-op run"}
    if not run.used_memory:
        return {"skipped": "run did not use memory"}

    memory = run.meta.get("memory") or {}
    character = memory.get("character")
    game_version = memory.get("game_version") or run.meta.get("game_build")
    if game_version == "unknown":
        game_version = None
    contexts = {s["step"]: context_from_state(s.get("state"), character, game_version) for s in run.steps}
    wasted = wasted_steps(run)
    started = _started(run)

    report = {"run": run.id, "scored_at": now(), "notes": {}}
    for note in store.live():
        if started and note.created and datetime.fromisoformat(note.created) > started:
            continue
        applies = {n: matches(note, ctx) for n, ctx in contexts.items()}
        shown_steps = sum(note.id in (s.get("notes_shown") or []) for s in run.steps)
        obs = observations(note, run, applies, wasted)
        if not obs and not shown_steps:
            continue
        note.shown_n += shown_steps
        for arm, loss in obs:
            getattr(note.utility, arm).add(loss)
        estimate(note.utility)
        entry = {"shown_steps": shown_steps, "units": [[arm, round(loss, 4)] for arm, loss in obs],
                 "mean": note.utility.mean, "lo": note.utility.lo, "hi": note.utility.hi}
        new_status = gate(note)
        if new_status:
            entry["status"] = [note.status, new_status]
            note.status = new_status
        note.updated = now()
        report["notes"][note.id] = entry
    marker.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
