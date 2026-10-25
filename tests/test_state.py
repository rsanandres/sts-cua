import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from sts_cua.state import fetch_state, summarize

COMBAT = {
    "state_type": "monster",
    "run": {"act": 1, "floor": 1, "ascension": 0},
    "battle": {"round": 3, "enemies": [{"name": "Nibbit", "hp": 1, "max_hp": 43, "block": 0,
                                         "intents": [{"type": "Attack", "label": "12", "title": "Aggressive"}]}]},
    "player": {"hp": 62, "max_hp": 80, "block": 0, "energy": 1, "gold": 99,
               "hand": [{"index": 0, "name": "Defend", "cost": "1", "target_type": "Self"},
                        {"index": 1, "name": "Strike", "cost": "1", "target_type": "AnyEnemy"}]},
}


def test_summarize_combat():
    s = summarize(COMBAT)
    assert s["state_type"] == "monster" and s["floor"] == 1 and s["round"] == 3
    assert s["hand"][1] == {"slot": 2, "name": "Strike", "cost": "1", "target": "AnyEnemy"}
    assert s["enemies"][0]["intents"] == ["12"]
    assert summarize(None) is None
    assert summarize({"state_type": "menu"}) == {"state_type": "menu"}


def test_fetch_state_from_server_and_unreachable():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(COMBAT).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert fetch_state(f"http://127.0.0.1:{server.server_port}/")["state_type"] == "monster"
    finally:
        server.shutdown()
    assert fetch_state("http://127.0.0.1:9/", timeout=0.5) is None


def test_summarize_scoring_fields():
    menu = {"state_type": "menu", "menu_screen": "main", "message": "Main menu.",
            "options": ["continue", "abandon_run", "singleplayer"]}
    assert summarize(menu) == {"state_type": "menu", "menu_screen": "main",
                               "menu_options": ["continue", "abandon_run", "singleplayer"]}
    tutorial = {"state_type": "menu", "menu_screen": "tutorial_prompt", "options": [{"name": "no", "enabled": True}]}
    assert summarize(tutorial)["menu_options"] == ["no"]

    s = summarize({**COMBAT, "player": {**COMBAT["player"], "character": "The Ironclad"}})
    assert s["ascension"] == 0 and s["character"] == "The Ironclad" and "combat_ended" not in s

    after = {"state_type": "boss", "message": "Combat ended. Waiting for rewards...",
             "run": {"act": 1, "floor": 17, "ascension": 0}, "player": {"hp": 40, "max_hp": 80}}
    assert summarize(after)["combat_ended"] is True and "enemies" not in summarize(after)

    game_map = {"state_type": "map", "run": {"act": 1, "floor": 3, "ascension": 0},
                "map": {"current_position": {"col": 3, "row": 2, "type": "Monster"},
                        "bosses": [{"col": 3, "row": 15, "id": "ENCOUNTER.VANTOM_BOSS", "name": "Vantom"}]}}
    assert summarize(game_map)["map_position"] == "Monster" and summarize(game_map)["bosses"] == ["Vantom"]
