import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from sts_cua.history import (
    HistoryEntry, character_key, entry_ids, fetch_history, find_run, parse_entry, parse_history, run_outcome,
)
from sts_cua.score import BOSS_POINTS, RunScore

START = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
T0 = int(START.timestamp())


def run_file(stem, start_time=None, win=False, abandoned=False, character="CHARACTER.IRONCLAD", points=7, **extra):
    """One run_history entry as BuildRunHistoryEntry writes it (McpMod.Compendium.cs @ 55e0648)."""
    written = datetime.fromtimestamp((start_time or int(stem)) + 1800, timezone.utc)
    return {
        "id": stem, "run_id": f"modded:profile3:{stem}", "file_name": f"{stem}.run", "size_bytes": 48211,
        "last_write_time_utc": written.strftime("%Y-%m-%dT%H:%M:%S.1234567Z"),
        "start_time": start_time if start_time is not None else int(stem), "run_time": 1790,
        "game_mode": "standard", "ascension": 0, "win": win, "was_abandoned": abandoned,
        "killed_by_encounter": "NONE.NONE" if win or abandoned else "ENCOUNTER.NIBBITS_WEAK",
        "killed_by_event": "NONE.NONE", "seed": f"SEED{stem[-4:]}", "build_id": "v0.107.1",
        "acts": ["ACT.OVERGROWTH"],
        "players": [{"id": 1, "character": character, "deck_count": 14, "relic_count": 2, "potion_count": 1}],
        "map_point_count": points, **extra,
    }


def compendium(*entries):
    return {
        "profile_id": 3, "source": "SaveManager.Progress and active-profile save files",
        "sections": {
            "card_library": {"ui_label": "Card Library", "status": "exposed", "discovered_ids": ["BASH"], "stats": []},
            "run_history": {
                "ui_label": "Run History", "status": "exposed" if entries else "exposed_empty",
                "source": "Profile save history files", "history_path": "/home/raph/.local/share/SlayTheSpire2/x",
                "entry_count": len(entries), "entries": list(entries),
                "limitation": "Run history is summarized from the active profile's saved .run files.",
            },
        },
    }


def test_parse_recorded_entry():
    entries = parse_history(compendium(run_file(str(T0 + 60), points=9)))
    (e,) = entries
    assert e.id == str(T0 + 60) and e.run_id == f"modded:profile3:{T0 + 60}" and e.start_time == T0 + 60
    assert e.last_write == datetime.fromtimestamp(T0 + 60 + 1800, timezone.utc) + timedelta(microseconds=123456)
    assert (e.win, e.abandoned, e.outcome, e.seed, e.floor, e.ascension) == (False, False, "died", f"SEED{str(T0 + 60)[-4:]}", 9, 0)
    assert e.character == "CHARACTER.IRONCLAD" and e.deck_count == 14 and e.relic_count == 2
    assert e.killed_by_encounter == "ENCOUNTER.NIBBITS_WEAK" and e.killed_by_event is None
    assert e.build_id == "v0.107.1" and e.acts == ["ACT.OVERGROWTH"] and e.run_time == 1790
    d = e.to_dict()
    assert d["outcome"] == "died" and d["last_write"].startswith("2026-10-25T18:31:00.123456")
    json.dumps(d)  # fits in score.json

    assert parse_entry(run_file("1", win=True)).outcome == "won"
    assert parse_entry(run_file("1", abandoned=True)).outcome == "abandoned"


def test_parse_odd_responses():
    assert parse_history(None) is None
    assert parse_history({"error": "No profile data available."}) is None
    assert parse_history({"profile_id": 3}) is None
    # No saves/history directory yet: entries is a dict of progress members.
    no_dir = {"sections": {"run_history": {"status": "exposed_empty", "entries": {}}}}
    assert parse_history(no_dir) == []
    # Unreadable file, and an entry without an id.
    broken = {"id": "1777000000", "file_name": "1777000000.run", "parse_error": "unexpected end of data"}
    assert [e.outcome for e in parse_history(compendium(broken, {"file_name": "x.run"}))] == [None]
    # Nulls are dropped by the mod, so missing keys are the normal case.
    sparse = parse_entry({"id": 5, "win": True})
    assert sparse.id == "5" and sparse.outcome == "won" and sparse.seed is None and sparse.characters == []
    assert entry_ids(None) is None and entry_ids([sparse]) == {"5"}


def test_fetch_history_from_server_and_unreachable():
    body = json.dumps(compendium(run_file(str(T0)))).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert [e.id for e in fetch_history(f"http://127.0.0.1:{server.server_port}/")] == [str(T0)]
    finally:
        server.shutdown()
    assert fetch_history("http://127.0.0.1:9/", timeout=0.5) is None


def test_character_key():
    assert character_key("CHARACTER.IRONCLAD") == character_key("The Ironclad") == "ironclad"
    assert character_key("The Silent") == "silent" and character_key(None) is None


def entries(*files):
    return parse_history(compendium(*files))


END = START + timedelta(minutes=40)
OLD = run_file(str(T0 - 7200))


def test_find_new_entry():
    before = entries(OLD)
    after = entries(run_file(str(T0 + 45)), OLD)
    found = find_run(after, START, END, seen_ids=entry_ids(before), character="The Ironclad", floor=7)
    assert found.id == str(T0 + 45)


def test_find_no_new_entry():
    before = entries(OLD)
    assert find_run(before, START, END, seen_ids=entry_ids(before)) is None
    assert find_run(None, START, END) is None
    # Without a snapshot, the old run is outside the time window.
    assert find_run(before, START, END) is None
    # A new id whose run started before this one (e.g. older than the snapshot's 20) isn't ours.
    stale = entries(run_file(str(T0 - 3600)), OLD)
    assert find_run(stale, START, END, seen_ids=entry_ids(before)) is None
    # Nor one whose file can't be read.
    unreadable = entries({"id": str(T0 + 45), "parse_error": "bad"}, OLD)
    assert find_run(unreadable, START, END, seen_ids=entry_ids(before)) is None


def test_find_two_candidates():
    seen = entry_ids(entries(OLD))
    ironclad, silent = run_file(str(T0 + 30), points=7), run_file(str(T0 + 90), character="CHARACTER.SILENT")
    # The character from the states picks the right one even though it's older.
    assert find_run(entries(silent, ironclad, OLD), START, END, seen, character="The Ironclad", floor=7).id == str(T0 + 30)
    # Same character: the floor decides; then the newest start_time.
    a, b = run_file(str(T0 + 30), points=7), run_file(str(T0 + 90), points=3)
    assert find_run(entries(b, a, OLD), START, END, seen, character="The Ironclad", floor=7).id == str(T0 + 30)
    assert find_run(entries(b, a, OLD), START, END, seen, character="The Ironclad").id == str(T0 + 90)
    # Without a snapshot, the window still keeps out the old run.
    assert find_run(entries(b, a, OLD), START, END, character="The Ironclad", floor=3).id == str(T0 + 90)


def state_result(outcome="died", floor=7, bosses=0):
    return RunScore(outcome=outcome, score=floor + BOSS_POINTS * bosses, max_floor=floor, act=1,
                    bosses_killed=bosses, character="The Ironclad", ascension=0, started=True)


def test_run_outcome():
    plain = run_outcome(state_result(), None)
    assert plain["outcome_source"] == "states" and plain["seed"] is None and plain["outcome_states"] is None
    assert plain["outcome"] == "died" and plain["score"] == 7 and plain["history"] is None

    agree = run_outcome(state_result(), parse_entry(run_file(str(T0))), "new_id")
    assert agree["outcome_source"] == "compendium" and agree["seed"] == f"SEED{str(T0)[-4:]}"
    assert agree["outcome_states"] is None and agree["max_floor_states"] is None and agree["history"]["match"] == "new_id"

    # The states guessed a Give Up; the game recorded a win on floor 51.
    won = run_outcome(state_result("abandoned", 50, 2), parse_entry(run_file(str(T0), win=True, points=51)))
    assert (won["outcome"], won["score"], won["max_floor"]) == ("won", 100, 51)
    assert won["outcome_states"] == "abandoned" and won["max_floor_states"] == 50 and won["bosses_killed"] == 2

    # The other way: the states saw a final-boss "win"; the game says died. Score falls back to the formula.
    died = run_outcome(state_result("won", 51, 3), parse_entry(run_file(str(T0), points=51)))
    assert died["outcome"] == "died" and died["outcome_states"] == "won"
    assert died["score"] == round(51 + 3 * BOSS_POINTS, 2)

    # No map points recorded: keep the states' floor.
    no_floor = run_outcome(state_result(), HistoryEntry(id="1", win=False, seed="S"))
    assert no_floor["max_floor"] == 7 and no_floor["max_floor_states"] is None
