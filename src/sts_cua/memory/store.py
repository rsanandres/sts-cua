"""Lesson notes on disk: one JSON note per line in memory/notes.jsonl.

A note is a short lesson the agent wrote after an earlier run. Its keys say when it
applies (see retrieve.py for the rule that keys only name things visible on screen).
Its utility says whether showing it helped (see score.py). Notes are never deleted,
only retired, so a run log's `notes_shown` ids always resolve.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = Path("memory/notes.jsonl")
KINDS = ("ui", "enemy", "card", "event", "strategy", "pitfall")
KEY_NAMES = ("screen", "enemy", "card", "event", "act", "character", "game_version")
STATUSES = ("probation", "active", "retired")
MAX_TEXT = 300


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Group:
    """Running count, mean and sum of squared deviations (Welford) of per-unit losses."""

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)


@dataclass
class Utility:
    """Loss when the note was eligible but hidden vs shown. mean = hidden - shown; > 0 means it helped.

    lo/hi bound the mean (score.Z standard errors); None until both groups have data.
    """

    shown: Group = field(default_factory=Group)
    hidden: Group = field(default_factory=Group)
    mean: float | None = None
    lo: float | None = None
    hi: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> Utility:
        return cls(
            shown=Group(**d.get("shown", {})), hidden=Group(**d.get("hidden", {})),
            mean=d.get("mean"), lo=d.get("lo"), hi=d.get("hi"),
        )


@dataclass
class Note:
    id: str
    kind: str
    text: str
    keys: dict[str, str] = field(default_factory=dict)
    status: str = "probation"
    shown_n: int = 0  # steps on which the note was shown to the agent
    utility: Utility = field(default_factory=Utility)
    downvotes: int = 0
    source_runs: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Note:
        d = dict(d)
        d["utility"] = Utility.from_dict(d.get("utility") or {})
        return cls(**d)


def clean_keys(keys: dict | None) -> dict[str, str]:
    """Known key names only, values as stripped strings, empty values dropped."""
    out = {}
    for name in KEY_NAMES:
        value = (keys or {}).get(name)
        if value is not None and str(value).strip():
            out[name] = str(value).strip()
    return out


def clip_text(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_TEXT:
        return text
    return text[: MAX_TEXT - 3].rsplit(" ", 1)[0] + "..."


class NoteStore:
    def __init__(self, path: str | Path = DEFAULT_PATH, notes: list[Note] | None = None):
        self.path = Path(path)
        self.notes = notes or []

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> NoteStore:
        path = Path(path)
        notes = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    notes.append(Note.from_dict(json.loads(line)))
        return cls(path, notes)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text("".join(json.dumps(n.to_dict()) + "\n" for n in self.notes), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, note_id: str) -> Note | None:
        return next((n for n in self.notes if n.id == note_id), None)

    def live(self) -> list[Note]:
        return [n for n in self.notes if n.status != "retired"]

    def add(self, kind: str, text: str, keys: dict | None = None, source_run: str | None = None) -> Note:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        numbers = [int(n.id[1:]) for n in self.notes if n.id[1:].isdigit()]
        stamp = now()
        note = Note(
            id=f"n{max(numbers, default=0) + 1:04d}", kind=kind, text=clip_text(text), keys=clean_keys(keys),
            source_runs=[source_run] if source_run else [], created=stamp, updated=stamp,
        )
        self.notes.append(note)
        return note
