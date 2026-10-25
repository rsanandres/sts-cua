"""Builders for summarized mod states (the shape state.summarize returns)."""


def menu(screen="main", options=("singleplayer", "settings", "quit")):
    return {"state_type": "menu", "menu_screen": screen, "menu_options": list(options)}


def room(kind, floor, act=1, hp=80, **extra):
    return {"state_type": kind, "act": act, "floor": floor, "ascension": 0, "character": "The Ironclad",
            "player": {"hp": hp, "max_hp": 80, "block": 0, "energy": None, "gold": 99}, **extra}


def fight(kind, floor, act, hp, *enemies):
    return room(kind, floor, act, hp, enemies=[{"name": n, "hp": 10, "max_hp": 10, "block": 0, "intents": []} for n in enemies])


def ended(kind, floor, act, hp):
    return room(kind, floor, act, hp, combat_ended=True)


def game_over(floor, act, hp):
    return room("game_over", floor, act, hp)
