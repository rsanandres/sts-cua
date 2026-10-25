"""Pick the notes to show on a step, in plain code (no model call).

Key rule: a note's keys may only name things the agent can see on screen at that
moment, so a note is never a side channel for hidden state:
- screen: the screen type (the mod's state_type, e.g. map, monster, rewards, event;
  "combat" matches any screen with enemies on it)
- enemy: an enemy name shown under the enemy
- card: a card name in the hand
- event: the event's title
- act: the act number in the top bar
- character, game_version: fixed for a run (the character picked; the pinned game build)
The harness reads these from the logged game state only because it already names
what is on screen; nothing else from the state (draw pile, intents' hidden numbers,
seeds) may become a key.

Matching: a note applies when every key it has matches. A missing screen, enemy,
card, event or act value never matches; an unknown character or game_version
(None) matches anything. Names compare case-insensitively, with a card's
upgrade "+" ignored.

Selection: active notes are shown whenever they apply, best utility first.
Probation notes are shown on a seeded coin flip (probation_rate) so score.py can
compare shown vs hidden. The coin is keyed by (seed, note, act, floor), so a whole
combat or screen visit is one A/B unit rather than flipping every step.
Strategy notes with no screen, enemy, card or event key form an always-on playbook
with its own budget. Tokens are estimated as characters / 4.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .store import Note, NoteStore

HEADER = (
    "Lessons from your earlier runs (notes written after past games; they can be wrong, "
    "and what you see on screen always wins):"
)
_SITUATIONAL_KEYS = ("screen", "enemy", "card", "event")


def estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _norm(name) -> str:
    return str(name).strip().casefold()


def _card(name) -> str:
    return _norm(name).rstrip("+").strip()


@dataclass
class Context:
    """What is on screen at a step, as the keys see it."""

    screen: str | None = None
    combat: bool = False
    enemies: set[str] = field(default_factory=set)
    cards: set[str] = field(default_factory=set)
    event: str | None = None
    act: str | None = None
    floor: str | None = None
    character: str | None = None
    game_version: str | None = None


def context_from_state(state: dict | None, character: str | None = None, game_version: str | None = None) -> Context:
    state = state or {}
    event = state.get("event")
    if isinstance(event, dict):
        event = event.get("name") or event.get("title")
    return Context(
        screen=_norm(state["state_type"]) if state.get("state_type") else None,
        combat=bool(state.get("enemies")),
        enemies={_norm(e["name"]) for e in state.get("enemies") or [] if e.get("name")},
        cards={_card(c["name"]) for c in state.get("hand") or [] if c.get("name")},
        event=_norm(event) if event else None,
        act=str(state["act"]) if state.get("act") is not None else None,
        floor=str(state["floor"]) if state.get("floor") is not None else None,
        character=_norm(character) if character else None,
        game_version=_norm(game_version) if game_version else None,
    )


def matches(note: Note, ctx: Context) -> bool:
    for name, value in note.keys.items():
        if name == "screen":
            want = _norm(value)
            if not (want == ctx.screen or (want == "combat" and ctx.combat)):
                return False
        elif name == "enemy" and _norm(value) not in ctx.enemies:
            return False
        elif name == "card" and _card(value) not in ctx.cards:
            return False
        elif name == "event" and _norm(value) != ctx.event:
            return False
        elif name == "act" and _norm(value) != ctx.act:
            return False
        elif name in ("character", "game_version"):
            have = getattr(ctx, name)
            if have is not None and _norm(value) != have:
                return False
    return True


def in_playbook(note: Note) -> bool:
    return note.kind == "strategy" and not any(k in note.keys for k in _SITUATIONAL_KEYS)


def eligible(notes: list[Note], ctx: Context) -> list[Note]:
    return [n for n in notes if n.status != "retired" and matches(n, ctx)]


def coin(seed: int, note_id: str, ctx: Context, rate: float) -> bool:
    unit = f"{ctx.act}:{ctx.floor}" if ctx.floor is not None else f"screen:{ctx.screen}"
    return random.Random(f"{seed}:{note_id}:{unit}").random() < rate


def _rank(note: Note) -> tuple:
    specificity = sum(k in note.keys for k in ("enemy", "card", "event")) * 2 + ("screen" in note.keys)
    return (-(note.utility.mean or 0.0), -specificity, note.id)


def _fill(notes: list[Note], budget_tokens: int) -> list[Note]:
    chosen, used = [], 0
    for note in notes:
        cost = estimate_tokens(note.text) + 1  # "- " and newline
        if used + cost > budget_tokens:
            continue
        chosen.append(note)
        used += cost
    return chosen


class Retriever:
    """notes_fn for loop.run: state -> (notes block text, ids of the notes shown)."""

    def __init__(
        self,
        store: NoteStore,
        seed: int,
        character: str | None = None,
        game_version: str | None = None,
        budget_tokens: int = 600,
        playbook_tokens: int = 1500,
        probation_rate: float = 0.5,
    ):
        self.notes = store.live()  # snapshot at run start; the store changes only after the run
        self.seed = seed
        self.character = character
        self.game_version = game_version
        self.budget_tokens = budget_tokens
        self.playbook_tokens = playbook_tokens
        self.probation_rate = probation_rate

    def select(self, state: dict | None) -> tuple[list[Note], list[Note]]:
        """(playbook notes, situational notes) to show for this state."""
        ctx = context_from_state(state, self.character, self.game_version)
        shown = [
            n for n in eligible(self.notes, ctx)
            if n.status == "active" or coin(self.seed, n.id, ctx, self.probation_rate)
        ]
        # Active first, ranked by utility; probation notes after, in id order.
        shown.sort(key=lambda n: (n.status != "active", *_rank(n)))
        playbook = _fill([n for n in shown if in_playbook(n)], self.playbook_tokens)
        situational = _fill([n for n in shown if not in_playbook(n)], self.budget_tokens)
        return playbook, situational

    def __call__(self, state: dict | None) -> tuple[str, list[str]]:
        playbook, situational = self.select(state)
        if not playbook and not situational:
            return "", []
        lines = [HEADER]
        if playbook:
            lines += ["Playbook:"] + [f"- {n.text}" for n in playbook]
        if situational:
            lines += ["For this screen:"] + [f"- {n.text}" for n in situational]
        return "\n".join(lines), [n.id for n in playbook + situational]
