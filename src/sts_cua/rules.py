"""General rules: deterministic co-op decisions taken before the model is asked.

Each step, decide(state) runs the RULES table in order on the summarized co-op state
(sts_cua.coop.summarize) and returns the first Ruling, or None to leave the step to the model.

A Ruling either acts at once, with no model call (`actions`; the step logs `source: "rule"`),
or, when the input needs a screen position the mod doesn't give, hands the model a one-line
instruction for this step (`directive`; the step logs `source: "model"` and `rule`).
Directive rules become acting rules once the button positions are confirmed on the PC
(docs/action-space.md, "Co-op").

Rules use the mod's state, so co-op is not pixel-only where a rule fires: the mod decides
whose move it is and how everyone voted, and directives pass that on to the model.

Shared votes (map, shared events): the agent votes last and matches the majority of the
other players' votes, for 2 to 4 players. Ties on the map go to MAP_PREFERENCE (rest
first when HP is low); tied events let the model pick among the tied options. The
treasure chest is a bid, not a vote: the agent bids last on a relic nobody else wants, so
no one has to duel for it.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from .actions import Action, Key, Wait
from .coop import waiting
from .state import COMBAT_TYPES

HOLD_SECONDS = 30.0  # longest single wait step; the loop ends it early when the wait reason changes
# Reasons from coop.waiting for which there is nothing to do but wait.
HOLD_REASONS = {
    "lobby_ready": "ready in the lobby; waiting for the other players",
    "lobby_starting": "everyone is ready; the run is starting",
    "enemy_turn": "the enemies' turn",
    "teammate_end_turn": "turn ended; waiting for the other players to end theirs",
    "turn_ending": "every player ended their turn",
    "knocked_out": "knocked out for the rest of this fight",
    "teammates_voting": "a vote is open; the agent votes last, after every other player",
    "map_vote": "voted on the map; waiting for the path",
    "event_vote": "voted on the event; waiting for the result",
    "treasure_bid": "picked a relic; waiting for the other players",
}
# Map ties: the first type in this order wins, then the leftmost node. With HP under
# LOW_HP of max, a rest site goes first. Names are the mod's MapPointType names.
MAP_PREFERENCE = ("Monster", "Unknown", "Shop", "RestSite", "Treasure", "Elite", "Boss")
LOW_HP = 0.5
TAKE_REWARDS = ("gold", "potion")  # the mod's reward type names (GetRewardTypeName)


@dataclass(frozen=True)
class Ruling:
    rule: str
    why: str
    actions: tuple[Action, ...] = ()
    directive: str = ""  # instead of actions: an instruction for this step's model call
    hold: bool = False  # actions is one Wait that the loop may end early when the wait reason changes


def majority(votes: list) -> list:
    """The most common votes among other players (several on a tie), in first-seen order. Unvoted (None) entries are ignored."""
    cast = [tuple(v) if isinstance(v, list) else v for v in votes if v is not None]
    if not cast:
        return []
    counts = Counter(cast)
    top = max(counts.values())
    return [v for v in dict.fromkeys(cast) if counts[v] == top]


def _hp_low(state: dict) -> bool:
    player = state.get("player") or {}
    hp, max_hp = player.get("hp"), player.get("max_hp")
    return isinstance(hp, (int, float)) and isinstance(max_hp, (int, float)) and max_hp > 0 and hp / max_hp < LOW_HP


def map_tiebreak(nodes: list[dict], low_hp: bool) -> dict:
    order = ("RestSite", *[t for t in MAP_PREFERENCE if t != "RestSite"]) if low_hp else MAP_PREFERENCE
    rank = {t: i for i, t in enumerate(order)}
    return min(nodes, key=lambda n: (rank.get(n.get("type"), len(order)), n.get("col") if n.get("col") is not None else 99))


def _words(name: str | None) -> str:
    """'RestSite' -> 'rest site'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name or "unknown").lower()


def _ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


# ---- the rules: each takes the summarized state and returns a Ruling or None

def wait_turn(state: dict) -> Ruling | None:
    """Not the agent's move (coop.waiting): wait, with no model call."""
    reason = waiting(state)
    if reason in HOLD_REASONS:
        return Ruling("wait_turn", f"{reason}: {HOLD_REASONS[reason]}", (Wait(HOLD_SECONDS),), hold=True)
    return None


def vote_map(state: dict) -> Ruling | None:
    """Every other player voted on the map and the agent hasn't: vote with the majority."""
    vote = (state.get("coop") or {}).get("map_vote")
    if not vote or vote.get("local_voted") or any(v is None for v in vote.get("others") or []):
        return None
    options = vote.get("options") or []
    if not options:
        return None
    tied = majority(vote.get("others") or [])
    by_coord = {(o.get("col"), o.get("row")): o for o in options}
    candidates = [by_coord[t] for t in tied if t in by_coord] or options
    target = candidates[0] if len(candidates) == 1 else map_tiebreak(candidates, _hp_low(state))
    k = options.index(target) + 1
    why = "matches the other players' majority" if len(candidates) == 1 else "tie broken by MAP_PREFERENCE"
    text = (f"Rule for this step: every other player has voted on the map. Vote for the {_words(target.get('type'))} "
            f"node, the {_ordinal(k)} of the {len(options)} nodes you can reach, counted from the left. "
            "Click it once, then wait.")
    return Ruling("vote_map", f"col {target.get('col')} row {target.get('row')}: {why}", directive=text)


def vote_event(state: dict) -> Ruling | None:
    """A shared event every other player voted on: choose the majority option."""
    vote = (state.get("coop") or {}).get("event_vote")
    if not vote or vote.get("local_voted") or any(v is None for v in vote.get("others") or []):
        return None
    options = {o.get("index"): o for o in vote.get("options") or [] if not o.get("locked")}
    tied = [t for t in majority(vote.get("others") or []) if t in options]
    if not tied:
        return None
    titles = [f'"{options[t].get("title")}"' for t in tied]
    if len(tied) == 1:
        text = f"Rule for this step: every other player has chosen in this shared event. Choose the same option: {titles[0]}."
    else:
        text = ("Rule for this step: the other players are split in this shared event. "
                f"Choose one of these options: {', '.join(titles)}.")
    return Ruling("vote_event", f"options {tied}", directive=text)


def bid_treasure(state: dict) -> Ruling | None:
    """Every other player picked a relic: take one nobody else wants."""
    bid = (state.get("coop") or {}).get("treasure_bid")
    if not bid or bid.get("local_voted") or any(v is None for v in bid.get("others") or []):
        return None
    taken = {v for v in bid.get("others") or []}
    free = [r for r in bid.get("options") or [] if r.get("index") not in taken]
    if not free:
        return None
    names = ", ".join(str(r.get("name")) for r in free)
    text = ("Rule for this step: the other players have picked their relics. Pick one they didn't take: "
            f"{names}.")
    return Ruling("bid_treasure", f"free relics {[r.get('index') for r in free]}", directive=text)


def end_turn(state: dict) -> Ruling | None:
    """The agent's turn in combat with no playable card in hand: end the turn with e."""
    coop = state.get("coop") or {}
    if state.get("state_type") not in COMBAT_TYPES or "turn" not in coop or waiting(state) is not None:
        return None
    playable = coop.get("playable")
    if playable is None or any(p is not False for p in playable):
        return None  # unknown, or something can still be played: the model decides
    return Ruling("end_turn", "no playable card in hand", (Key("e"),))


def take_rewards(state: dict) -> Ruling | None:
    """Gold or potions on the rewards screen: take them."""
    items = ((state.get("coop") or {}).get("rewards") or {}).get("items") or []
    takes = [i for i in items if i.get("type") in TAKE_REWARDS]
    if not takes:
        return None
    parts = [f"{i['gold']} gold" if i.get("type") == "gold" and i.get("gold") is not None
             else (i.get("potion") or "a potion") if i.get("type") == "potion" else "gold"
             for i in takes]
    text = (f"Rule for this step: take the {', '.join(parts)} on the rewards screen by clicking each of them "
            "(skip a potion if all your potion slots are full).")
    return Ruling("take_rewards", f"{len(takes)} gold/potion rewards", directive=text)


def proceed(state: dict) -> Ruling | None:
    """Nothing left to choose: rewards all taken, or an event whose only option is to proceed."""
    coop = state.get("coop") or {}
    rewards = coop.get("rewards")
    if rewards is not None and not rewards.get("items") and rewards.get("can_proceed"):
        return Ruling("proceed", "rewards screen is empty",
                      directive="Rule for this step: nothing is left on the rewards screen. Click Proceed.")
    options = [o for o in coop.get("event_options") or [] if not o.get("locked")]
    if options and all(o.get("proceed") for o in options):
        return Ruling("proceed", "the event's only option is to proceed",
                      directive=f'Rule for this step: the event is over. Choose "{options[0].get("title") or "Proceed"}".')
    return None


def lobby_ready(state: dict) -> Ruling | None:
    """In the teammate's lobby and not ready yet: keep the default character and ready up."""
    lobby = (state.get("coop") or {}).get("lobby")
    if not lobby or lobby.get("local_ready"):
        return None
    return Ruling("lobby_ready", "not ready in the lobby",
                  directive="Rule for this step: you are in your teammate's co-op lobby. Keep the selected character "
                            "and press the confirm button to ready up. Never press Back.")


# Order matters: the first rule that fires wins.
RULES: list[tuple[str, Callable[[dict], Ruling | None]]] = [
    ("wait_turn", wait_turn),
    ("vote_map", vote_map),
    ("vote_event", vote_event),
    ("bid_treasure", bid_treasure),
    ("end_turn", end_turn),
    ("take_rewards", take_rewards),
    ("proceed", proceed),
    ("lobby_ready", lobby_ready),
]


def decide(state: dict | None) -> Ruling | None:
    """The first rule that fires on this state, or None (the model decides). No state, no rules."""
    if not state:
        return None
    for _, rule in RULES:
        ruling = rule(state)
        if ruling is not None:
            return ruling
    return None
