"""Read-only client for the STS2MCP mod's game state (ground truth for logging and scoring).

The agent never sees this state; only the harness and the run log do.
Mod: https://github.com/Gennadiyev/STS2MCP, built from main @ 55e0648 for Spire 2 v0.107.1.

Schema facts, read in that commit:
- `McpMod.cs` (`HandleGetState`): `GET /api/v1/singleplayer` returns JSON by default.
- `McpMod.StateBuilder.cs` (`BuildGameState`):
  - No run in progress -> `state_type` "menu", with `menu_screen` "main", "singleplayer",
    "character_select", "profile_select", "settings", "compendium", "timeline", or a
    multiplayer screen, and `options`. Main-menu options are plain strings and include
    "continue" and "abandon_run" only while a saved run exists. Menu states carry no
    `run` or `player`.
  - In a run, the top overlay wins: "card_select", "bundle_select", "relic_select",
    "crystal_sphere", "card_reward", "rewards", "game_over" (`NGameOverScreen`, message
    "Run ended.", options ["main_menu"]), or "overlay" for any other overlay.
  - Otherwise by room: "map" when the map is open; combat rooms report
    `RoomType.ToString().ToLower()`, i.e. "monster", "elite" or "boss", or "hand_select"
    while a card in hand is being chosen; "event", "fake_merchant", "shop", "rest_site",
    "treasure"; "unknown" otherwise. After a fight, before rewards, the combat state_type
    stays but `battle` is missing and `message` is "Combat ended. Waiting for rewards...".
  - Every in-run state has `run` {act (1-based), floor (`RunState.TotalFloor`), ascension}
    and `player` (`BuildPlayerState`: character title, hp, max_hp, block, gold; energy and
    hand only during combat).
  - `battle.enemies` lists only living enemies (`BuildBattleState`).
  - `map.current_position.type` is the `MapPointType` name; `map.boss` / `map.bosses`
    name the act's boss encounter(s); a second boss exists when the map has one
    (`BuildMapState`).
- `McpMod.Helpers.cs` (`BuildVisibleFtueState`): tutorial prompts and popups, in or out of
  a run, return `state_type` "menu" with `menu_screen` "tutorial_prompt", "tutorial" or
  "popup" and no `run`.
- `docs/raw-full.md`: the same schema with examples.
- Not in the singleplayer state: the seed, and whether a run was won. The saved run
  history (`win`, `was_abandoned`, `seed`) is only in `GET /api/v1/compendium`
  (`McpMod.Compendium.cs`, `BuildRunHistoryEntry`).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:15526/api/v1/singleplayer"
COMBAT_TYPES = ("monster", "elite", "boss")


def fetch_state(url: str = DEFAULT_URL, timeout: float = 2.0) -> dict | None:
    """GET the current game state. Returns None if the mod isn't reachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return None


def _option_name(option) -> str | None:
    return option.get("name") if isinstance(option, dict) else option


def summarize(state: dict | None) -> dict | None:
    """The fields worth keeping per step: screen, floor, HP, energy, hand, enemies, plus what scoring needs."""
    if not state:
        return None
    out = {"state_type": state.get("state_type")}
    if state.get("menu_screen"):
        out["menu_screen"] = state["menu_screen"]
        out["menu_options"] = [_option_name(o) for o in state.get("options") or []]
    run = state.get("run") or {}
    if run:
        out["act"], out["floor"] = run.get("act"), run.get("floor")
        out["ascension"] = run.get("ascension")
    player = state.get("player") or {}
    if player:
        out["character"] = player.get("character")
        out["player"] = {k: player.get(k) for k in ("hp", "max_hp", "block", "energy", "gold")}
        out["hand"] = [
            {"slot": c.get("index", 0) + 1, "name": c.get("name"), "cost": c.get("cost"), "target": c.get("target_type")}
            for c in player.get("hand") or []
        ]
    battle = state.get("battle") or {}
    if battle:
        out["round"] = battle.get("round")
        out["enemies"] = [
            {
                "name": e.get("name"), "hp": e.get("hp"), "max_hp": e.get("max_hp"), "block": e.get("block"),
                "intents": [i.get("label") or i.get("title") for i in e.get("intents") or []],
            }
            for e in battle.get("enemies") or []
        ]
    elif out["state_type"] in COMBAT_TYPES and run:
        out["combat_ended"] = True
    game_map = state.get("map") or {}
    if game_map:
        out["map_position"] = (game_map.get("current_position") or {}).get("type")
        out["bosses"] = [b.get("name") or b.get("id") for b in game_map.get("bosses") or []]
    return out
