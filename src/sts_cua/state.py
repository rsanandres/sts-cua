"""Read-only client for the STS2MCP mod's game state (ground truth for logging and scoring).

The agent never sees this state; only the harness and the run log do.
Mod: https://github.com/Gennadiyev/STS2MCP, built from main @ 55e0648 for Spire 2 v0.107.1.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:15526/api/v1/singleplayer"


def fetch_state(url: str = DEFAULT_URL, timeout: float = 2.0) -> dict | None:
    """GET the current game state. Returns None if the mod isn't reachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
        return None


def summarize(state: dict | None) -> dict | None:
    """The fields worth keeping per step: screen, floor, HP, energy, hand, enemies."""
    if not state:
        return None
    out = {"state_type": state.get("state_type")}
    run = state.get("run") or {}
    if run:
        out["act"], out["floor"] = run.get("act"), run.get("floor")
    player = state.get("player") or {}
    if player:
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
    return out
