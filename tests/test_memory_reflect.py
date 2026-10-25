import json

import pytest
from anthropic.types import Message
from memory_runs import fight, screen, step, write_run

from sts_cua.memory.reflect import MAX_ADDS, OPS_SCHEMA, apply_ops, build_request, reflect
from sts_cua.memory.runs import load_run
from sts_cua.memory.store import NoteStore

OLD = "2026-01-01T00:00:00+00:00"


class FakeClient:
    def __init__(self, ops):
        self.requests = []
        self.messages = self
        self.reply = Message.model_validate({
            "id": "msg_reflect", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": json.dumps({"ops": ops})}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 3000, "output_tokens": 400, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        })

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.reply


def setup(tmp_path):
    store = NoteStore(tmp_path / "notes.jsonl")
    for kind, keys, text, status in [
        ("enemy", {"enemy": "Nibbit"}, "Nibbit attacks for 12 on its first turn; play Defend before attacking.", "probation"),
        ("ui", {"screen": "map"}, "On the map, click the pulsing node, not the legend.", "active"),
        ("pitfall", {"screen": "combat"}, "Pressing a card number twice deselects it.", "active"),
    ]:
        note = store.add(kind, text, keys)
        note.status, note.created = status, OLD
    store.save()
    steps = [step(screen("map"), shown=["n0002"], error="click missed", thought="The map legend looks clickable.")]
    steps += fight("Nibbit", 1, 80, 18, shown=["n0001", "n0003"])
    steps[2]["thought"] = "Nibbit intends 12; I'll attack anyway."
    run_dir = write_run(tmp_path, "20261025T120000Z", steps, summary={"end_reason": "max_steps"})
    return store, load_run(run_dir)


def add(text, keys, kind="enemy"):
    return {"op": "ADD", "kind": kind, "keys": keys, "text": text, "reason": "seen this run"}


OPS = [
    add("Nibbit attacks for 12 on its first turn: play Defend before attacking!", {"enemy": "Nibbit"}),  # duplicate of n0001
    add("Cultist buffs on turn 1; attack it immediately.", {"enemy": "Cultist"}),
    add("On rewards, take the gold first.", {"screen": "rewards"}, "ui"),
    add("Bash applies Vulnerable; play it before Strikes.", {"card": "Bash"}, "card"),
    add("A fourth add that goes over the cap.", {"screen": "map"}, "ui"),
    {"op": "UPDATE", "id": "n0001", "text": "Nibbit hits for 12 on turn 1. Defend first, then Strike.", "reason": "lost 18 HP"},
    {"op": "DOWNVOTE", "id": "n0002", "reason": "agent still clicked the legend"},
    {"op": "RETIRE", "id": "n0003", "reason": "wrong"},
    {"op": "RETIRE", "id": "n0099", "reason": "no such note"},
]


def test_reflect_applies_delta_ops_and_logs_cost(tmp_path):
    store, run = setup(tmp_path)
    client = FakeClient(OPS)
    record = reflect(store, run, client=client)

    req = client.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": OPS_SCHEMA}
    digest = req["messages"][0]["content"]
    assert "| 1 | 1 | 1 | monster | Nibbit | 80 | 62 | 18 | won | 3 |" in digest
    assert "step 1 [map] error" in digest and "legend looks clickable" in digest
    assert "Nibbit intends 12" in digest
    assert "n0001 [enemy; enemy=Nibbit; probation; shown on 3 steps" in digest
    assert "Screen types seen: map, monster, rewards." in digest

    results = [r["result"] for r in record["results"]]
    assert results == [
        "skipped: duplicate of n0001", "added", "added", "added", f"skipped: at most {MAX_ADDS} ADDs per run",
        "updated", "downvoted (1), now probation", "retired", "skipped: unknown or retired id",
    ]
    saved = NoteStore.load(tmp_path / "notes.jsonl")
    assert [n.id for n in saved.notes] == ["n0001", "n0002", "n0003", "n0004", "n0005", "n0006"]
    assert saved.get("n0004").keys == {"enemy": "Cultist"} and saved.get("n0004").source_runs == [run.id]
    assert saved.get("n0004").status == "probation"
    assert saved.get("n0001").text.startswith("Nibbit hits for 12") and run.id in saved.get("n0001").source_runs
    assert saved.get("n0002").status == "probation" and saved.get("n0003").status == "retired"

    logged = json.loads((run.dir / "memory_reflect.json").read_text())
    assert logged["cost_usd"] == pytest.approx((3000 * 2.00 + 400 * 10.00) / 1e6)
    assert logged["usage"]["input_tokens"] == 3000 and len(logged["ops"]) == len(OPS)
    assert (run.dir / "memory_score.json").exists()  # reflect scores the run first


def test_adds_are_deduped_against_each_other_and_validated(tmp_path):
    store = NoteStore(tmp_path / "notes.jsonl")
    results = apply_ops(store, [
        add("Cultist buffs on turn 1; attack it now.", {"enemy": "Cultist"}),
        add("Cultist buffs on turn one: attack it now", {"enemy": "cultist"}),
        add("Cultist buffs on turn 1; attack it now.", {"enemy": "Cultist", "act": "1"}),  # different keys: kept
        {"op": "ADD", "kind": "nonsense", "text": "x", "reason": ""},
        {"op": "UPDATE", "id": "n0001", "reason": "no text"},
    ], "run1")
    assert [r["result"] for r in results] == [
        "added", "skipped: duplicate of n0001", "added", "skipped: needs a valid kind and text", "skipped: UPDATE needs text",
    ]


def test_three_downvotes_retire(tmp_path):
    store = NoteStore(tmp_path / "notes.jsonl")
    store.add("ui", "x", {"screen": "map"}).status = "active"
    for _ in range(3):
        apply_ops(store, [{"op": "DOWNVOTE", "id": "n0001", "reason": ""}], "r")
    assert store.get("n0001").status == "retired" and store.get("n0001").downvotes == 3


def test_schema_is_strict():
    def walk(schema):
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            for sub in schema["properties"].values():
                walk(sub)
        if schema.get("type") == "array":
            walk(schema["items"])

    walk(OPS_SCHEMA)


def test_bad_reply_changes_nothing(tmp_path):
    store, run = setup(tmp_path)
    client = FakeClient([])
    client.reply.content[0].text = "not json"
    record = reflect(store, run, client=client)
    assert "unparseable" in record["error"] and record["results"] == []
    assert len(NoteStore.load(tmp_path / "notes.jsonl").notes) == 3


def test_dry_run_request_has_no_side_effects(tmp_path):
    store, run = setup(tmp_path)
    request = build_request(store, run)
    assert "Combats" in request["messages"][0]["content"]
    assert not (run.dir / "memory_reflect.json").exists()


def test_reset_runs_are_skipped(tmp_path):
    store, _ = setup(tmp_path)
    reset = load_run(write_run(tmp_path, "reset1", fight("Nibbit", 1, 80, 5, shown=["n0001"]), meta={"kind": "reset"}))
    client = FakeClient(OPS)
    assert reflect(store, reset, client=client)["skipped"] == "reset run"
    assert client.requests == [] and not (reset.dir / "memory_score.json").exists()
