"""Read-only client for the STS2MCP mod's run history, the source of truth for how a run ended.

score.py infers a run's outcome from the per-step singleplayer states, which say nothing
about seeds or wins. The game itself records every finished run in a history file, and the
mod serves a summary of those files at `GET /api/v1/compendium`. The supervisor snapshots the
history before each game run and looks up the new entry after it (find_run).
Mod: https://github.com/Gennadiyev/STS2MCP, built from main @ 55e0648 for Spire 2 v0.107.1.

Schema facts, read in that commit:
- `McpMod.cs`: `GET /api/v1/compendium` -> `HandleGetCompendium`. `_jsonOptions` drops null
  values (`WhenWritingNull`), so a missing key means null. Its snake_case policy does not apply
  to dictionary keys; every key below is written literally.
- `McpMod.Compendium.cs`:
  - `BuildCompendiumResponse`: `{"profile_id", "current_run", "source", "sections": {...,
    "run_history"}}`, or `{"error": "No profile data available."}` without a profile.
  - `BuildRunHistorySection`: when the profile's `saves/history` directory exists,
    `run_history` is `{"status": "exposed" | "exposed_empty", "history_path", "entry_count",
    "entries": [...]}` with the 20 newest `*.run` files by last write time, newest first.
    Without that directory, `entries` is a dict of reflected progress members, not a list
    (read here as an empty history).
  - `BuildRunHistoryEntry`, one per `.run` file:
    - From the file: `id` (file name without `.run`), `run_id`
      ("{modded|vanilla}:profile{N}:{id}"), `file_name`, `size_bytes`, `last_write_time_utc`
      (a .NET DateTime, ISO 8601 UTC).
    - Copied as-is from the file's top level when present: `start_time`, `run_time`,
      `game_mode`, `ascension`, `win`, `was_abandoned`, `killed_by_encounter`,
      `killed_by_event`, `seed`, `build_id`; `acts` (a list).
    - `players`: [{id, character, deck_count, relic_count, potion_count}]. Only counts of
      the deck, relics and potions, not their contents.
    - `map_point_count`: the total length of the file's `map_point_history` arrays (one per act).
    - `parse_error` instead of the copied fields when the file isn't valid JSON.
  - `BuildCurrentRunContext`: `current_run` (only while a run is in progress) reads
    `current_run.save`: start_time, seed (`rng.seed`), run_time, ascension and a `run_id` in the
    same "{scope}:profile{N}:{start_time}" format, which is why the history `id` is taken to be
    the run's start_time.
- `docs/raw-full.md` ("GET /api/v1/compendium"): the example has `start_time` 1778295706,
  `seed` "2450ZAR9EF", `id` "1774869148", `players[].character` "CHARACTER.IRONCLAD",
  `run_time` 6541. It says several attempts can share a seed, so `id`, not the seed, names
  one run.

Assumptions, not confirmed from the mod source (the `.run` format is the game's):
- `start_time` is Unix seconds when the run began (the doc example's values), and `run_time`
  is seconds played.
- A run that ended in death, a win or Give Up has written its `.run` file by the time the
  game-over screen shows. The supervisor polls a few times in case it lands a moment later.
- `win` is true only for a won run; `was_abandoned` is true for Give Up / Abandon Run; any
  other finished run was a death.
- `map_point_count` equals the floor reached (`RunState.TotalFloor` in the singleplayer
  state), one map point per floor. score.json keeps the states' floor in `max_floor_states`
  when the two differ, so this can be checked.
- `killed_by_encounter` / `killed_by_event` may hold a placeholder such as "NONE.NONE" when
  nothing killed the player; such values are read as null.
- `players[].character` is an id like "CHARACTER.IRONCLAD"; the singleplayer state has the
  title ("The Ironclad"). character_key() compares them by the last word.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .score import RunScore, points

DEFAULT_URL = "http://localhost:15526/api/v1/compendium"
# How far start_time may sit before the harness's run start (clock skew between the
# game and the harness, which share a machine).
SLACK_SECONDS = 120.0


@dataclass
class HistoryEntry:
    id: str
    run_id: str | None = None
    start_time: int | None = None  # Unix seconds (assumption)
    last_write: datetime | None = None
    run_time: int | None = None  # seconds played (assumption)
    win: bool | None = None
    abandoned: bool | None = None
    seed: str | None = None
    characters: list[str] = field(default_factory=list)  # one per player, e.g. "CHARACTER.IRONCLAD"
    ascension: int | None = None
    floor: int | None = None  # map_point_count
    game_mode: str | None = None
    build_id: str | None = None
    killed_by_encounter: str | None = None
    killed_by_event: str | None = None
    acts: list = field(default_factory=list)
    deck_count: int | None = None  # first player's
    relic_count: int | None = None
    parse_error: str | None = None

    @property
    def character(self) -> str | None:
        return self.characters[0] if self.characters else None

    @property
    def outcome(self) -> str | None:
        """won, abandoned or died; None when the file couldn't be read."""
        if self.parse_error or self.win is None:
            return None
        if self.win:
            return "won"
        return "abandoned" if self.abandoned else "died"

    def to_dict(self) -> dict:
        """The fields worth keeping in score.json (small enough for the run page)."""
        out = asdict(self)
        out["last_write"] = self.last_write.isoformat() if self.last_write else None
        out["outcome"] = self.outcome
        return out


def fetch_compendium(url: str = DEFAULT_URL, timeout: float = 5.0) -> dict | None:
    """GET the compendium. Returns None if the mod isn't reachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return None


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _killer(value) -> str | None:
    text = _str(value)
    if text is None or text.split(".")[-1].upper() in ("NONE", "NULL", ""):
        return None
    return text


def _time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    # .NET writes up to 7 fractional digits; Python 3.11 takes at most 6.
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def parse_entry(raw: dict) -> HistoryEntry | None:
    """One run_history entry; None if it has no id."""
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    players = [p for p in raw.get("players") or [] if isinstance(p, dict)]
    first = players[0] if players else {}
    return HistoryEntry(
        id=str(raw["id"]), run_id=_str(raw.get("run_id")), start_time=_int(raw.get("start_time")),
        last_write=_time(raw.get("last_write_time_utc")), run_time=_int(raw.get("run_time")),
        win=_bool(raw.get("win")), abandoned=_bool(raw.get("was_abandoned")),
        seed=None if raw.get("seed") is None else str(raw["seed"]),
        characters=[c for c in (_str(p.get("character")) for p in players) if c],
        ascension=_int(raw.get("ascension")), floor=_int(raw.get("map_point_count")),
        game_mode=_str(raw.get("game_mode")), build_id=_str(raw.get("build_id")),
        killed_by_encounter=_killer(raw.get("killed_by_encounter")), killed_by_event=_killer(raw.get("killed_by_event")),
        acts=list(raw.get("acts") or []), deck_count=_int(first.get("deck_count")),
        relic_count=_int(first.get("relic_count")), parse_error=_str(raw.get("parse_error")),
    )


def parse_history(compendium: dict | None) -> list[HistoryEntry] | None:
    """The run history entries, newest first as the mod sends them. None if the response isn't usable."""
    if not isinstance(compendium, dict) or compendium.get("error"):
        return None
    sections = compendium.get("sections")
    if not isinstance(sections, dict) or not isinstance(sections.get("run_history"), dict):
        return None
    entries = sections["run_history"].get("entries")
    if not isinstance(entries, list):
        return []  # no saves/history directory yet: progress members, not runs
    return [e for e in (parse_entry(r) for r in entries) if e is not None]


def fetch_history(url: str = DEFAULT_URL, timeout: float = 5.0) -> list[HistoryEntry] | None:
    return parse_history(fetch_compendium(url, timeout))


def entry_ids(entries: list[HistoryEntry] | None) -> set[str] | None:
    """The snapshot taken before a run; None when the history couldn't be read."""
    return None if entries is None else {e.id for e in entries}


def character_key(name: str | None) -> str | None:
    """'CHARACTER.IRONCLAD' and 'The Ironclad' both -> 'ironclad'."""
    if not name:
        return None
    words = re.findall(r"[a-z0-9]+", name.lower().split(".")[-1])
    return words[-1] if words else None


def find_run(
    entries: list[HistoryEntry] | None,
    started_at: datetime,
    until: datetime,
    seen_ids: set[str] | None = None,
    character: str | None = None,
    floor: int | None = None,
    slack_seconds: float = SLACK_SECONDS,
) -> HistoryEntry | None:
    """The history entry for a run that started at started_at and was looked up at until.

    A candidate is an entry the pre-run snapshot didn't have (any entry when there was no
    snapshot) with a readable outcome, a start_time inside [started_at - slack, until], and,
    when the mod gave one, a last write after started_at - slack. Among candidates, ones whose
    character and floor agree with the states come first, then the newest start_time.
    """
    lo, hi = started_at.timestamp() - slack_seconds, until.timestamp() + slack_seconds
    want_char = character_key(character)

    def fits(e: HistoryEntry) -> bool:
        if seen_ids is not None and e.id in seen_ids:
            return False
        if e.outcome is None or e.start_time is None or not lo <= e.start_time <= hi:
            return False
        return e.last_write is None or e.last_write.timestamp() >= lo

    def rank(e: HistoryEntry) -> tuple:
        char_ok = want_char is None or not e.characters or want_char in {character_key(c) for c in e.characters}
        floor_ok = floor is None or e.floor is None or e.floor == floor
        return (char_ok, floor_ok, e.start_time, e.last_write.timestamp() if e.last_write else 0.0)

    candidates = [e for e in (entries or []) if fits(e)]
    return max(candidates, key=rank) if candidates else None


def run_outcome(result: RunScore, entry: HistoryEntry | None, match: str | None = None) -> dict:
    """score.json's contents: the state-based score, overridden by the history entry when there is one.

    With an entry, outcome, max_floor, seed and ascension come from it (outcome_source
    "compendium") and the score is recomputed from them with the states' boss kills. The states'
    outcome and floor are kept in outcome_states / max_floor_states when they disagree. Without one,
    the states' result stands (outcome_source "states", seed null).
    """
    out = {**result.to_dict(), "seed": None, "outcome_source": "states", "outcome_states": None,
           "max_floor_states": None, "history": None}
    if entry is None:
        return out
    floor = entry.floor if entry.floor else result.max_floor
    out.update(
        outcome=entry.outcome, max_floor=floor, score=points(entry.outcome, floor, result.bosses_killed),
        seed=entry.seed, outcome_source="compendium", history={**entry.to_dict(), "match": match},
    )
    if entry.ascension is not None:
        out["ascension"] = entry.ascension
    if entry.outcome != result.outcome:
        out["outcome_states"] = result.outcome
    if floor != result.max_floor:
        out["max_floor_states"] = result.max_floor
    return out
