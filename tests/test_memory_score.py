import json

from memory_runs import CLICK, combat, fight, screen, step, write_run
from PIL import Image

from sts_cua.memory.runs import combats, load_run, wasted_steps
from sts_cua.memory.score import MIN_HIDDEN, MIN_SHOWN, estimate, gate, score_run
from sts_cua.memory.store import NoteStore, Utility

OLD = "2026-01-01T00:00:00+00:00"


def store_with(tmp_path, *notes):
    store = NoteStore(tmp_path / "notes.jsonl")
    for kind, keys, status in notes:
        note = store.add(kind, f"{kind} note", keys)
        note.status, note.created = status, OLD
    return store


def ab_fights(note_id, shown_losses, hidden_losses, enemy="Nibbit"):
    steps, floor = [], 1
    for loss in shown_losses:
        steps += fight(enemy, floor, 80, loss, shown=[note_id])
        floor += 1
    for loss in hidden_losses:
        steps += fight(enemy, floor, 80, loss, shown=[])
        floor += 1
    return steps


def test_combats_from_logged_states(tmp_path):
    steps = [step(screen("map"))] + fight("Nibbit", 1, 80, 12) + [step(None)] + fight("Cultist", 2, 68, 68)[:-1]
    steps[-1]["state"]["player"]["hp"] = 0
    run = load_run(write_run(tmp_path, "r", steps))
    first, second = combats(run.steps)
    assert (first.enemies, first.floor, first.hp_start, first.hp_end, first.hp_lost, first.result) == \
        (["Nibbit"], 1, 80, 68, 12, "won")
    assert len(first.steps) == 3
    assert (second.enemies, second.hp_lost, second.result) == (["Cultist"], 68, "lost")


def test_wasted_steps(tmp_path):
    steps = [step(screen("map")), step(screen("map"), error="boom"), step(screen("map"), executed=[]),
             step(screen("map")), step(screen("map"))]
    run_dir = write_run(tmp_path, "r", steps)
    (run_dir / "screenshots").mkdir()
    for n, color in enumerate([(0, 0, 0), (9, 9, 9), (50, 50, 50), (90, 90, 90), (90, 90, 90)], 1):
        Image.new("RGB", (160, 90), color).save(run_dir / f"screenshots/step_{n:04d}.png")
    assert wasted_steps(load_run(run_dir)) == {2: "error", 3: "no action", 4: "screen unchanged"}


def test_estimate_and_gate():
    u = Utility()
    estimate(u)
    assert u.mean is None
    for x in (0.05, 0.06, 0.04, 0.05, 0.05):
        u.shown.add(x)
    for x in (0.25, 0.3, 0.2):
        u.hidden.add(x)
    estimate(u)
    assert abs(u.mean - 0.2) < 1e-3 and 0 < u.lo < u.mean < u.hi


def test_helpful_combat_note_is_promoted(tmp_path):
    store = store_with(tmp_path, ("enemy", {"enemy": "Nibbit"}, "probation"), ("enemy", {"enemy": "Cultist"}, "probation"))
    steps = ab_fights("n0001", [4, 6, 5, 3, 7, 5], [20, 24, 18, 22])
    steps += fight("Cultist", 20, 80, 30, shown=[])  # n0002 applies here only, hidden
    run = load_run(write_run(tmp_path, "r1", steps))

    report = score_run(store, run)
    note = store.get("n0001")
    assert note.utility.shown.n == 6 and note.utility.hidden.n == 4
    assert note.shown_n == 18  # 3 combat steps per shown fight
    assert abs(note.utility.mean - (21 - 5) / 80) < 1e-3
    assert note.utility.lo > 0 and note.status == "active"
    assert report["notes"]["n0001"]["status"] == ["probation", "active"]
    other = store.get("n0002")
    assert other.utility.hidden.n == 1 and other.utility.shown.n == 0 and other.status == "probation"
    assert (run.dir / "memory_score.json").exists()

    # Scored once: a second call changes nothing.
    assert score_run(store, run)["skipped"] == "already scored"
    assert store.get("n0001").utility.shown.n == 6


def test_harmful_note_is_retired_and_not_enough_data_waits(tmp_path):
    store = store_with(tmp_path, ("strategy", {"screen": "combat"}, "active"))
    steps = ab_fights("n0001", [30, 28, 32, 31, 29], [5, 6, 4])
    run = load_run(write_run(tmp_path, "r1", steps))
    score_run(store, run)
    assert store.get("n0001").status == "retired" and store.get("n0001").utility.hi < 0

    few = store_with(tmp_path / "b", ("enemy", {"enemy": "Nibbit"}, "probation"))
    run = load_run(write_run(tmp_path / "b", "r1", ab_fights("n0001", [1] * (MIN_SHOWN - 1), [40] * MIN_HIDDEN)))
    score_run(few, run)
    assert few.get("n0001").utility.lo > 0 and gate(few.get("n0001")) is None
    assert few.get("n0001").status == "probation"


def test_ui_note_scored_on_wasted_steps_per_visit(tmp_path):
    store = store_with(tmp_path, ("ui", {"screen": "map"}, "probation"))
    steps = []
    for visit in range(8):
        shown = visit % 2 == 0
        steps.append(step(screen("map", floor=visit), shown=["n0001"] if shown else []))
        steps.append(step(screen("map", floor=visit), shown=["n0001"] if shown else [],
                          error=None if shown else "click missed"))
        steps.append(step(combat("Nibbit", 80, floor=visit), shown=[]))
    run = load_run(write_run(tmp_path, "r1", steps))
    score_run(store, run)
    u = store.get("n0001").utility
    assert (u.shown.n, u.hidden.n) == (4, 4)
    assert u.shown.mean == 0 and u.hidden.mean == 0.5


def test_skips_new_notes_and_runs_without_memory(tmp_path):
    store = store_with(tmp_path, ("enemy", {"enemy": "Nibbit"}, "probation"))
    store.get("n0001").created = "2027-01-01T00:00:00+00:00"  # written after the run started
    run = load_run(write_run(tmp_path, "r1", ab_fights("n0001", [5], [20])))
    assert score_run(store, run)["notes"] == {}

    plain = write_run(tmp_path, "r2", [step(combat("Nibbit", 80), executed=[CLICK])], meta={"memory": None})
    meta = json.loads((plain / "meta.json").read_text())
    del meta["memory"]
    (plain / "meta.json").write_text(json.dumps(meta))
    assert score_run(store, load_run(plain)) == {"skipped": "run did not use memory"}


def test_score_cli(tmp_path, capsys):
    from sts_cua.memory.__main__ import main

    store = store_with(tmp_path, ("enemy", {"enemy": "Nibbit"}, "probation"))
    store.save()
    run_dir = write_run(tmp_path, "r1", ab_fights("n0001", [4, 6, 5, 3, 7], [20, 24, 18]))
    main(["score", str(run_dir)])  # store path comes from the run's meta
    assert NoteStore.load(tmp_path / "notes.jsonl").get("n0001").status == "active"
    assert '"n0001"' in capsys.readouterr().out
    main(["list", "--store", str(tmp_path / "notes.jsonl")])
    assert "n0001  active" in capsys.readouterr().out
    main(["retire", "n0001", "--store", str(tmp_path / "notes.jsonl")])
    main(["show", "n0001", "--store", str(tmp_path / "notes.jsonl")])
    assert '"status": "retired"' in capsys.readouterr().out
    main(["list", "--store", str(tmp_path / "notes.jsonl")])
    assert "n0001" not in capsys.readouterr().out
