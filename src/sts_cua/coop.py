"""Co-op mode: the agent plays a Spire 2 run with a human teammate who hosts.

    python -m sts_cua --coop --display :99 --model claude-haiku-5-5

Setup and the join flow are in README.md ("Co-op") and docs/hosting.md. The agent never
accepts an invite itself: it starts once its game already shows the host's lobby.

Mod facts, read in STS2MCP main @ 55e0648 (https://github.com/Gennadiyev/STS2MCP):
- `McpMod.cs`: `GET /api/v1/singleplayer` answers 409 while a multiplayer run is in
  progress, and `GET /api/v1/multiplayer` answers 409 outside one. Lobbies are not runs,
  so the lobby comes from the singleplayer endpoint. fetch_state tries both.
- `McpMod.StateBuilder.cs` (`AddCharacterSelectMenuState`, `BuildStartRunLobbyState`):
  character select in a co-op lobby carries `lobby` {type host|client, all_ready,
  is_local_ready, players [{is_local, character_id, is_ready, ...}]}, and the options add
  "unready" (take back ready) next to "back" (leave the lobby).
  `AddMultiplayerLoadLobbyMenuState`: menu_screen "multiplayer_load_lobby" (resuming a
  saved co-op run) has `lobby` {all_ready, players [{is_local, is_ready, is_connected}]}.
- `McpMod.MultiplayerState.cs` (`BuildMultiplayerGameState`): the same state types as
  singleplayer plus `game_mode` "multiplayer", `players` [{character, is_local, hp,
  max_hp, is_alive, and in combat is_ready_to_end_turn}], `battle` {turn, is_play_phase,
  all_players_ready}, `map` {votes [{is_local, voted}], all_voted}, a shared `event`
  {is_shared, votes, all_voted} and `treasure` {is_bidding_phase, bids, all_bid}.
  There is no game_over check in that builder, so (inferred, not seen) the game-over
  screen shows as state_type "overlay" with overlay.screen_type "NGameOverScreen".
- `README.md`: the mod's own multiplayer support is beta.

The harness reads these fields only to log them and to tell the loop when waiting for the
teammate is normal (waiting()). The model never sees them; it stays pixel-only.
"""

from __future__ import annotations

from . import state as solo

MODE = "coop"
IDLE_MINUTES = 30.0  # co-op: stop after this long with no action of the agent's that changed the screen
MAX_IDLE_STEPS = 10  # wait-only steps in a row while the mod says it is the agent's move
MAX_STEPS = 5000  # a rule's wait is one step of up to 30 s, so waiting doesn't eat the cap
# Speed: never be the player everyone waits on. Haiku 5.5 with thinking off (the model
# defaults), a short history and almost no settle time between steps.
SETTLE_SECONDS = 0.3
HISTORY_STEPS = 4

GOAL = (
    "Play this co-op run of Slay the Spire 2 with your human teammate, all the way to the end, "
    "and help the team win."
)

# Appended to the system prompt only in co-op, so the single-player prompt (and its cache
# prefix) never changes. No coordinates or key syntax here: both adapters use it as is.
PROMPT = """

Co-op: you are playing this run together with a human teammate, who plays their own character on their own computer and hosts the game.
- Lobby: pick your character and confirm to ready up, then wait for the run to start. Never press Back in the lobby or leave it: that disconnects you from your teammate.
- Combat: you control only your own character, energy and hand. When you have nothing useful left to play, end your turn with e. The turn only passes once every player has ended theirs, so after ending yours, wait until your next turn starts. Don't press e or End Turn again while you wait; it can take back your end turn.
- Map: every player votes on the next node. Vote last: wait until your teammates have voted, then click the node most of them chose, once, and wait for the game to pick the path. Never vote first.
- Shared events work the same way: choose after your teammates, with the majority. In a treasure chest, take a relic your teammates didn't pick.
- Rewards, shops, rest sites and most events are separate for each player: make your own choices, proceed, then wait for your teammate.
- Never touch your teammate's choices, and never choose Give Up, Save and Quit, Abandon or Back out of the run.
- Waiting for your teammate is normal and can take minutes. When it isn't your move, use wait (about 5 to 10 seconds) instead of clicking.
- The run is over only when the game says so; don't say the goal is complete before that."""


def multiplayer_url(url: str) -> str:
    """The mod's multiplayer endpoint next to a singleplayer one."""
    base = url.rstrip("/")
    if base.endswith("/singleplayer"):
        return base[: -len("singleplayer")] + "multiplayer"
    return base + "/multiplayer" if not base.endswith("/multiplayer") else base


def singleplayer_url(url: str) -> str:
    base = url.rstrip("/")
    if base.endswith("/multiplayer"):
        return base[: -len("multiplayer")] + "singleplayer"
    return base


def fetch_state(url: str = solo.DEFAULT_URL, timeout: float = 2.0) -> dict | None:
    """The co-op run's state, else the singleplayer endpoint's (menus and lobbies). None if the mod is down.

    Each endpoint answers 409 when the other one applies; fetch_state reads that as None.
    """
    return (solo.fetch_state(multiplayer_url(url), timeout)
            or solo.fetch_state(singleplayer_url(url), timeout))


def _votes(entries, key) -> dict | None:
    """A vote's local and other players' choices. `key` names the chosen-target field(s) in each entry."""
    if not isinstance(entries, list) or not entries:
        return None
    entries = [e for e in entries if isinstance(e, dict)]

    def choice(e):
        if not e.get("voted"):
            return None
        return [e.get(k) for k in key] if isinstance(key, tuple) else e.get(key)

    local = next((e for e in entries if e.get("is_local")), None)
    others = [e for e in entries if not e.get("is_local")]
    return {
        "local_voted": bool(local and local.get("voted")),
        "all_voted": all(e.get("voted") for e in entries),
        "others": [choice(e) for e in others],  # None: not voted yet
    }


def _lobby(lobby: dict) -> dict:
    players = [p for p in lobby.get("players") or [] if isinstance(p, dict)]
    local = next((p for p in players if p.get("is_local")), None)
    local_ready = lobby.get("is_local_ready")
    if local_ready is None:
        local_ready = bool(local and local.get("is_ready"))
    return {
        "type": lobby.get("type"),
        "local_ready": bool(local_ready),
        "all_ready": bool(lobby.get("all_ready")),
        "players": [{"is_local": bool(p.get("is_local")), "character": p.get("character_id") or p.get("character"),
                     "ready": bool(p.get("is_ready")), **({"connected": p["is_connected"]} if "is_connected" in p else {})}
                    for p in players],
    }


def summarize(state: dict | None) -> dict | None:
    """state.summarize plus a `coop` block: teammates, ready flags, votes and the choices on screen.

    waiting() and sts_cua.rules read the block; the model never sees it.
    """
    out = solo.summarize(state)
    if out is None:
        return None
    overlay = state.get("overlay") or {}
    if out.get("state_type") == "overlay" and overlay.get("screen_type") == "NGameOverScreen":
        out["state_type"] = "game_over"  # inferred: the multiplayer builder has no game_over check
    elif overlay.get("screen_type"):
        out["overlay"] = overlay["screen_type"]

    coop: dict = {}
    if isinstance(state.get("lobby"), dict):
        coop["lobby"] = _lobby(state["lobby"])
    players = [p for p in state.get("players") or [] if isinstance(p, dict)]
    if players:
        coop["players"] = [
            {"character": p.get("character"), "is_local": bool(p.get("is_local")), "hp": p.get("hp"),
             "max_hp": p.get("max_hp"), "alive": p.get("is_alive"),
             **({"ready": p["is_ready_to_end_turn"]} if "is_ready_to_end_turn" in p else {})}
            for p in players
        ]
    battle = state.get("battle") or {}
    if battle and state.get("game_mode") == "multiplayer":
        coop["turn"] = battle.get("turn")
        coop["play_phase"] = battle.get("is_play_phase")
        coop["all_ready"] = battle.get("all_players_ready")
        hand = (state.get("player") or {}).get("hand")
        if isinstance(hand, list):
            coop["playable"] = [c.get("can_play") for c in hand if isinstance(c, dict)]
    game_map = state.get("map") or {}
    if (votes := _votes(game_map.get("votes"), ("vote_col", "vote_row"))) is not None:
        votes["options"] = [{"col": o.get("col"), "row": o.get("row"), "type": o.get("type")}
                            for o in game_map.get("next_options") or [] if isinstance(o, dict)]
        coop["map_vote"] = votes
    event = state.get("event") or {}
    if event.get("is_shared") and (votes := _votes(event.get("votes"), "vote_option")) is not None:
        votes["options"] = [{"index": o.get("index"), "title": o.get("title"), "locked": bool(o.get("is_locked"))}
                            for o in event.get("options") or [] if isinstance(o, dict)]
        coop["event_vote"] = votes
    if event.get("options") and not event.get("is_shared"):
        coop["event_options"] = [{"title": o.get("title"), "proceed": bool(o.get("is_proceed")),
                                  "locked": bool(o.get("is_locked"))}
                                 for o in event["options"] if isinstance(o, dict)]
    treasure = state.get("treasure") or {}
    if treasure.get("is_bidding_phase") and (bids := _votes(treasure.get("bids"), "vote_relic_index")) is not None:
        bids["options"] = [{"index": r.get("index"), "name": r.get("name")}
                           for r in treasure.get("relics") or [] if isinstance(r, dict)]
        coop["treasure_bid"] = bids
    rewards = state.get("rewards") or {}
    if rewards:
        coop["rewards"] = {
            "items": [{"type": i.get("type"), "gold": i.get("gold_amount"), "potion": i.get("potion_name")}
                      for i in rewards.get("items") or [] if isinstance(i, dict)],
            "can_proceed": bool(rewards.get("can_proceed")),
        }
    if coop or state.get("game_mode") == "multiplayer":
        out["coop"] = coop
    return out


def _local(state: dict) -> dict:
    players = (state.get("coop") or {}).get("players") or []
    return next((p for p in players if p.get("is_local")), {})


VOTES = (("map_vote", "map_vote"), ("event_vote", "event_vote"), ("treasure_bid", "treasure_bid"))


def waiting(state: dict | None) -> str | None:
    """Why waiting for the teammate (or the game) is normal now, or None when it is the agent's move.

    A summarized co-op state goes in (summarize above). None comes back only where the mod
    shows it is the agent's move: its own turn in combat before it pressed End Turn, a vote
    every other player has already cast while it hasn't (the agent votes last, see
    sts_cua.rules), a lobby it hasn't readied in, or a menu outside any lobby. Everywhere else
    the mod can't tell, for example rewards, shops, rest sites and private events, where the
    agent may be done and waiting for the teammate; those give "teammate_maybe" so the run
    never ends there for idling.
    """
    if state is None:
        return "no_state"
    coop = state.get("coop") or {}
    lobby = coop.get("lobby")
    if lobby:
        if lobby.get("local_ready") and not lobby.get("all_ready"):
            return "lobby_ready"
        if lobby.get("all_ready"):
            return "lobby_starting"
        return None
    if state.get("state_type") == "menu":
        return None if state.get("menu_screen") not in ("tutorial_prompt", "tutorial", "popup") else "teammate_maybe"
    if state.get("state_type") == "game_over":
        return None
    if "turn" in coop:
        if coop.get("turn") not in (None, "player") or coop.get("play_phase") is False:
            return "enemy_turn"
        if _local(state).get("alive") is False:
            return "knocked_out"
        if _local(state).get("ready"):
            return "teammate_end_turn" if not coop.get("all_ready") else "turn_ending"
        return None
    for key, reason in VOTES:
        vote = coop.get(key)
        if vote:
            if vote.get("local_voted"):
                return reason
            if any(v is None for v in vote.get("others") or []):
                return "teammates_voting"  # the agent never leads a vote
            return None
    return "teammate_maybe"


def in_session(state: dict | None) -> bool:
    """A co-op lobby or run (what the solo supervisor must leave alone)."""
    return bool(state) and bool(state.get("multiplayer") or state.get("coop") is not None)


def is_coop(meta: dict | None) -> bool:
    return bool(meta) and meta.get("mode") == MODE
