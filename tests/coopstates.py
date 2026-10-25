"""Builders for raw STS2MCP co-op states (the shapes McpMod.MultiplayerState.cs and
McpMod.StateBuilder.cs return), so tests run them through coop.summarize like the harness."""


def players(local_ready=False, others_ready=(False,), local_alive=True):
    out = [{"character": "The Ironclad", "is_local": True, "hp": 70, "max_hp": 80, "gold": 99,
            "is_alive": local_alive, "is_ready_to_end_turn": local_ready}]
    for i, ready in enumerate(others_ready):
        out.append({"character": f"Teammate {i + 1}", "is_local": False, "hp": 60, "max_hp": 66, "gold": 80,
                    "is_alive": True, "is_ready_to_end_turn": ready})
    return out


def run_info(floor=3):
    return {"act": 1, "floor": floor, "ascension": 0}


def player(hand=None, hp=70):
    p = {"character": "The Ironclad", "hp": hp, "max_hp": 80, "block": 0, "gold": 99, "energy": 3}
    if hand is not None:
        p["hand"] = hand
    return p


def card(index, name="Strike", can_play=True):
    return {"index": index, "name": name, "cost": "1", "target_type": "AnyEnemy", "can_play": can_play}


def combat(local_ready=False, others_ready=(False,), all_ready=False, turn="player", play_phase=True,
           hand=None, local_alive=True):
    hand = [card(0), card(1, "Defend")] if hand is None else hand
    return {
        "state_type": "monster", "game_mode": "multiplayer", "net_type": "SteamMultiplayer",
        "player_count": 1 + len(others_ready), "local_player_slot": 0, "run": run_info(),
        "battle": {"round": 2, "turn": turn, "is_play_phase": play_phase, "all_players_ready": all_ready,
                   "enemies": [{"name": "Nibbit", "hp": 20, "max_hp": 43, "block": 0, "intents": []}]},
        "players": players(local_ready, others_ready, local_alive), "player": player(hand),
    }


def vote(is_local, col=None, row=None, voted=None):
    voted = col is not None if voted is None else voted
    return {"player": "x", "is_local": is_local, "voted": voted, "vote_col": col if voted else None,
            "vote_row": row if voted else None}


NODES = [{"index": 0, "col": 1, "row": 4, "type": "Monster"}, {"index": 1, "col": 3, "row": 4, "type": "RestSite"},
         {"index": 2, "col": 5, "row": 4, "type": "Elite"}]


def map_state(local=None, others=((3, 4),), nodes=NODES, hp=70):
    """others: each teammate's (col, row) vote, or None for not voted yet."""
    votes = [vote(True, *(local or (None, None)))]
    votes += [vote(False, *(o or (None, None))) for o in others]
    return {
        "state_type": "map", "game_mode": "multiplayer", "run": run_info(), "player": player(hp=hp),
        "players": players(others_ready=[False] * len(others)),
        "map": {"current_position": {"col": 3, "row": 3, "type": "Monster"}, "next_options": nodes,
                "votes": votes, "all_voted": all(v["voted"] for v in votes)},
    }


def event_state(local=None, others=(0,), shared=True):
    options = [{"index": 0, "title": "Take the gold", "is_locked": False, "is_proceed": False},
               {"index": 1, "title": "Leave", "is_locked": False, "is_proceed": False}]
    event = {"event_name": "A Shared Thing", "is_shared": shared, "options": options}
    if shared:
        event["votes"] = ([{"is_local": True, "voted": local is not None, "vote_option": local}]
                          + [{"is_local": False, "voted": o is not None, "vote_option": o} for o in others])
    return {"state_type": "event", "game_mode": "multiplayer", "run": run_info(), "player": player(),
            "players": players(), "event": event}


def treasure_state(local=None, others=(0,)):
    return {
        "state_type": "treasure", "game_mode": "multiplayer", "run": run_info(), "player": player(),
        "players": players(),
        "treasure": {"is_bidding_phase": True,
                     "relics": [{"index": 0, "name": "Anchor"}, {"index": 1, "name": "Lantern"}],
                     "bids": ([{"is_local": True, "voted": local is not None, "vote_relic_index": local}]
                              + [{"is_local": False, "voted": o is not None, "vote_relic_index": o} for o in others])},
    }


def rewards_state(items=(("gold", 17), ("potion", "Fire Potion"), ("card", None)), can_proceed=True):
    out = []
    for kind, value in items:
        item = {"index": len(out), "type": kind, "description": kind}
        if kind == "gold":
            item["gold_amount"] = value
        if kind == "potion":
            item["potion_name"] = value
        out.append(item)
    return {"state_type": "rewards", "game_mode": "multiplayer", "run": run_info(), "player": player(),
            "players": players(), "rewards": {"items": out, "can_proceed": can_proceed}}


def lobby(local_ready=False, all_ready=False, others_ready=(False,)):
    """Character select in a co-op lobby (from the singleplayer endpoint: no run in progress yet)."""
    ps = [{"id": "1", "slot_id": 0, "is_local": True, "is_host": False, "character_id": "IRONCLAD",
           "is_ready": local_ready, "platform_name": "budthud"}]
    ps += [{"id": str(i + 2), "slot_id": i + 1, "is_local": False, "is_host": False, "character_id": "SILENT",
            "is_ready": r, "platform_name": "Raph"} for i, r in enumerate(others_ready)]
    return {"state_type": "menu", "menu_screen": "character_select",
            "options": [{"name": "IRONCLAD", "enabled": True}, {"name": "confirm", "enabled": True},
                        {"name": "back", "enabled": True}, {"name": "unready", "enabled": local_ready}],
            "lobby": {"type": "client", "game_mode": "standard", "max_players": 4, "ascension": 0,
                      "all_ready": all_ready, "is_about_to_begin": all_ready, "is_local_ready": local_ready,
                      "players": ps, "player_count": len(ps)}}


def game_over_overlay():
    return {"state_type": "overlay", "game_mode": "multiplayer", "run": run_info(9), "player": player(hp=0),
            "players": players(), "overlay": {"screen_type": "NGameOverScreen", "message": "..."}}
