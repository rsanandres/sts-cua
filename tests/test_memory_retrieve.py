import json

from memory_runs import combat, screen

from sts_cua.memory.retrieve import HEADER, Retriever, context_from_state, estimate_tokens, matches
from sts_cua.memory.store import MAX_TEXT, Note, NoteStore


def make_store(tmp_path, notes):
    store = NoteStore(tmp_path / "notes.jsonl")
    for kind, keys, text, status in notes:
        note = store.add(kind, text, keys)
        note.status = status
    return store


def test_store_round_trip(tmp_path):
    store = NoteStore(tmp_path / "mem" / "notes.jsonl")
    a = store.add("enemy", "Nibbit hits for 12 on turn 1: play Defend first.", {"enemy": "Nibbit", "act": 1, "bogus": "x"}, "run1")
    b = store.add("ui", "x " * 400, {"screen": "map", "card": ""})
    a.utility.shown.add(0.1)
    a.utility.shown.add(0.3)
    store.save()

    loaded = NoteStore.load(tmp_path / "mem" / "notes.jsonl")
    assert [n.id for n in loaded.notes] == ["n0001", "n0002"]
    first = loaded.get("n0001")
    assert first == a
    assert first.keys == {"enemy": "Nibbit", "act": "1"}  # unknown and empty keys dropped, values as strings
    assert first.status == "probation" and first.source_runs == ["run1"] and first.created
    assert first.utility.shown.n == 2 and abs(first.utility.shown.mean - 0.2) < 1e-9
    assert len(loaded.get("n0002").text) <= MAX_TEXT and b.keys == {"screen": "map"}
    lines = (tmp_path / "mem" / "notes.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["id"] == "n0001"
    assert NoteStore.load(tmp_path / "missing.jsonl").notes == []
    assert loaded.add("card", "c").id == "n0003"


def test_matching_uses_screen_names_cards_and_act():
    ctx = context_from_state(combat("Nibbit", 70, act=1, hand=("Strike+", "Bash")), game_version="v0.107.1")

    def note(**keys):
        return Note(id="n1", kind="enemy", text="t", keys={k: str(v) for k, v in keys.items()})

    assert matches(note(enemy="nibbit"), ctx)
    assert matches(note(screen="monster", card="Strike", act=1), ctx)  # upgrade "+" ignored
    assert matches(note(screen="combat"), ctx)
    assert matches(note(character="Ironclad"), ctx)  # character unknown -> matches
    assert matches(note(game_version="v0.107.1"), ctx)
    assert not matches(note(game_version="v0.106.0"), ctx)
    assert not matches(note(enemy="Cultist"), ctx)
    assert not matches(note(card="Defend"), ctx)
    assert not matches(note(act=2), ctx)
    assert not matches(note(screen="map"), ctx)
    assert not matches(note(event="Neow"), ctx)
    map_ctx = context_from_state(screen("map"))
    assert matches(note(screen="map"), map_ctx) and not matches(note(screen="combat"), map_ctx)
    assert not matches(note(enemy="Nibbit"), context_from_state(None))


def test_active_notes_filtered_ranked_and_capped(tmp_path):
    store = make_store(tmp_path, [
        ("enemy", {"enemy": "Nibbit"}, "Nibbit low", "active"),
        ("enemy", {"enemy": "Nibbit"}, "Nibbit high", "active"),
        ("enemy", {"enemy": "Cultist"}, "Cultist note", "active"),
        ("ui", {"screen": "map"}, "Map note", "active"),
        ("card", {"card": "Bash"}, "Bash retired", "retired"),
        ("strategy", {}, "Playbook note", "active"),
    ])
    store.get("n0001").utility.mean = 0.01
    store.get("n0002").utility.mean = 0.2
    retriever = Retriever(store, seed=0)
    text, ids = retriever(combat("Nibbit", 70, hand=("Bash",)))
    assert ids == ["n0006", "n0002", "n0001"]  # playbook first, then by utility
    assert text.startswith(HEADER)
    assert text.index("Playbook note") < text.index("Nibbit high") < text.index("Nibbit low")
    assert "Cultist" not in text and "Map note" not in text and "Bash" not in text
    assert retriever(screen("map"))[1] == ["n0006", "n0004"]

    long = make_store(tmp_path, [("enemy", {"enemy": "Nibbit"}, f"{i} " + "y" * 290, "active") for i in range(10)]
                      + [("strategy", {}, f"{i} " + "z" * 290, "active") for i in range(10)])
    playbook, situational = Retriever(long, seed=0, budget_tokens=600, playbook_tokens=1500).select(combat("Nibbit", 70))
    assert sum(estimate_tokens(n.text) + 1 for n in situational) <= 600 and len(situational) == 8  # 74 tokens each
    assert len(playbook) == 10  # the playbook has its own, larger budget
    capped, _ = Retriever(long, seed=0, playbook_tokens=300).select(combat("Nibbit", 70))
    assert len(capped) == 4


def test_empty_store_shows_nothing(tmp_path):
    assert Retriever(NoteStore(tmp_path / "n.jsonl"), seed=0)(combat("Nibbit", 70)) == ("", [])


def test_probation_shown_on_seeded_half_of_floors(tmp_path):
    store = make_store(tmp_path, [("enemy", {"enemy": "Nibbit"}, "probation note", "probation")])
    retriever = Retriever(store, seed=42)
    shown = [bool(retriever(combat("Nibbit", 70, floor=f))[1]) for f in range(1, 201)]
    assert 70 < sum(shown) < 130
    # The same floor always gets the same decision, so a whole combat is one A/B unit.
    again = [bool(retriever(combat("Nibbit", 50, floor=f, hand=("Defend",)))[1]) for f in range(1, 201)]
    assert again == shown
    assert [bool(Retriever(store, seed=42)(combat("Nibbit", 70, floor=f))[1]) for f in range(1, 201)] == shown
    other = [bool(Retriever(store, seed=7)(combat("Nibbit", 70, floor=f))[1]) for f in range(1, 201)]
    assert other != shown
    assert not any(Retriever(store, seed=42, probation_rate=0.0)(combat("Nibbit", 70, floor=f))[1] for f in range(1, 50))


def test_main_memory_flag_wires_retriever(tmp_path, frame, monkeypatch, capsys):
    import sts_cua.__main__ as cli
    from sts_cua.screen import FakeScreen

    store = make_store(tmp_path, [("strategy", {}, "Block when the enemy attacks.", "active")])
    store.save()
    monkeypatch.setattr(cli, "X11Screen", lambda display: FakeScreen([frame]))
    cli.main(["--model", "scripted", "--no-state", "--max-steps", "2", "--settle", "0",
              "--runs-dir", str(tmp_path / "runs"), "--memory", str(store.path)])
    run_dir = next((tmp_path / "runs").iterdir())
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["memory"]["path"] == str(store.path) and isinstance(meta["memory"]["seed"], int)
    steps = [json.loads(line) for line in (run_dir / "steps.jsonl").read_text().splitlines()]
    assert [s["notes_shown"] for s in steps] == [["n0001"], ["n0001"]]
