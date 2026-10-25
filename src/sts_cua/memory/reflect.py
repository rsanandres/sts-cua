"""Post-run editor: one Claude call turns a run log into delta operations on the note store.

The model sees a compact digest of the run (per-combat table, outcome, wasted or
erroring steps, its own thoughts at key moments, the notes it was shown with their
stats, and nearby existing notes) and returns only ADD (at most MAX_ADDS),
UPDATE, DOWNVOTE and RETIRE operations as JSON matching OPS_SCHEMA. The store is
never rewritten wholesale. Cost goes to memory_reflect.json in the run directory.
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

from .. import pricing
from ..score import score_run as score_outcome
from ..actions import Click, Drag, Key, MouseMove, Scroll, TypeText, Wait, describe
from ..models.base import Usage
from .retrieve import context_from_state, matches
from .runs import Run, fights, wasted_steps
from .score import score_run
from .store import KEY_NAMES, KINDS, MAX_TEXT, NoteStore, clean_keys, clip_text, now

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_ADDS = 3
DOWNVOTES_TO_RETIRE = 3
SIMILARITY = 0.6  # normalized text similarity at or above which an ADD duplicates a note with the same keys
REFLECT_FILE = "memory_reflect.json"
MAX_PROBLEM_STEPS = 20
MAX_NEARBY_NOTES = 40
THOUGHT_CHARS = 240

SYSTEM_PROMPT = f"""You maintain a small store of lessons for an agent that plays Slay the Spire 2 from screenshots. After each run you read a digest of the run and edit the store with a few delta operations, so the agent does better next time. Each note is shown to the agent during play when its keys match the screen.

Operations:
- ADD: a new lesson (at most {MAX_ADDS} per run). Needs kind, keys and text.
- UPDATE: rewrite an existing note's text (and optionally its keys) when this run showed it was incomplete or slightly wrong.
- DOWNVOTE: an existing note that was shown and misled the agent or did not help.
- RETIRE: an existing note that is wrong or no longer applies.
Return an empty list when nothing new was learned. Prefer UPDATE over adding a near-duplicate.

Rules for notes:
- Text at most {MAX_TEXT} characters: one concrete, actionable lesson, written to the agent ("When ..., do ...").
- General lessons only: nothing about this run's seed, map layout or random rewards.
- Kind is one of: {", ".join(KINDS)}. ui = how to operate a screen; pitfall = an input mistake to avoid; enemy, card, event = how to handle that thing; strategy = general play.
- Keys say when the note is shown. Use only what the agent can see on screen: screen (a screen type from the digest, or "combat"), enemy (an enemy name), card (a card name in hand), event (an event title), act (a number). Leave a key empty when it does not apply. Use game_version only for notes about exact screen positions. A strategy note with no screen, enemy, card or event key is shown on every step, so keep those rare and short.
- Base lessons on evidence in the digest: HP lost, wasted steps, errors, and what your thoughts show you misread."""

_KEYS_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "string"} for name in KEY_NAMES},
    "additionalProperties": False,
}
OPS_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["ADD", "UPDATE", "DOWNVOTE", "RETIRE"]},
                    "id": {"type": "string", "description": "existing note id; empty for ADD"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "keys": _KEYS_SCHEMA,
                    "text": {"type": "string"},
                    "reason": {"type": "string", "description": "the evidence from this run, one sentence"},
                },
                "required": ["op", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ops"],
    "additionalProperties": False,
}

_ACTIONS = {cls.kind: cls for cls in (Click, Drag, MouseMove, Scroll, Key, TypeText, Wait)}


def _describe(action: dict) -> str:
    cls = _ACTIONS.get(action.get("kind"))
    if cls is None:
        return str(action)
    return describe(cls(**{k: v for k, v in action.items() if k != "kind"}))


def _clip(text: str | None, limit: int = THOUGHT_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _note_line(note, extra: str = "") -> str:
    keys = ", ".join(f"{k}={v}" for k, v in note.keys.items()) or "no keys"
    u = note.utility
    stats = f"utility {u.mean} [{u.lo}, {u.hi}] from {u.shown.n} shown / {u.hidden.n} hidden units" if u.mean is not None \
        else f"no utility yet ({u.shown.n} shown / {u.hidden.n} hidden units)"
    return f"- {note.id} [{note.kind}; {keys}; {note.status}; shown on {note.shown_n} steps; {stats}{extra}]: {note.text}"


def digest(store: NoteStore, run: Run) -> str:
    """The run as compact text for the editor."""
    steps = run.steps
    fought = fights(run)
    outcome = score_outcome(run.states)
    wasted = wasted_steps(run)
    screens = sorted({s["state"]["state_type"] for s in steps if (s.get("state") or {}).get("state_type")})
    last_state = next((s["state"] for s in reversed(steps) if s.get("state")), None) or {}
    player = last_state.get("player") or {}
    out = [
        f"Run {run.id}: model {run.meta.get('model')}, game build {run.meta.get('game_build')}.",
        f"Outcome: {outcome.outcome}, ended by {run.summary.get('end_reason', 'unknown')} after {len(steps)} steps; "
        f"max floor {outcome.max_floor}; bosses killed {outcome.bosses_killed}; score {outcome.score}; "
        f"last screen {last_state.get('state_type')}; HP {player.get('hp')}/{player.get('max_hp')}.",
        f"Screen types seen: {', '.join(screens) or 'none logged'}.",
        "",
        "Combats (HP is the player's):",
        "| # | act | floor | room | enemies | HP start | HP end | HP lost | result | steps |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for f in fought:
        c = f.combat
        out.append(f"| {f.index} | {c.act} | {c.floor} | {c.room} | {', '.join(c.enemies)} | {c.hp_start} | {c.hp_end} | "
                   f"{c.hp_lost} | {c.result} | {c.steps} |")
    if not fought:
        out.append("(no combats logged)")

    by_number = {s["step"]: s for s in steps}
    out += ["", f"Wasted or erroring steps ({len(wasted)} of {len(steps)}):"]
    for number in sorted(wasted)[:MAX_PROBLEM_STEPS]:
        s = by_number[number]
        done = "; ".join(_describe(a) for a in s.get("executed") or []) or "no action"
        screen = (s.get("state") or {}).get("state_type")
        error = f" error: {_clip(s.get('error'), 160)}." if s.get("error") else ""
        out.append(f"- step {number} [{screen}] {wasted[number]}: {done}.{error} Thought: {_clip(s.get('thought'))}")
    if len(wasted) > MAX_PROBLEM_STEPS:
        out.append(f"- ... {len(wasted) - MAX_PROBLEM_STEPS} more")

    key_steps: dict[int, str] = {}
    for f in fought:
        key_steps.setdefault(f.steps[0], f"start of combat {f.index}")
        hp = [((by_number[n].get("state") or {}).get("player") or {}).get("hp") for n in f.steps]
        drops = [(hp[i] - hp[i + 1], f.steps[i]) for i in range(len(hp) - 1) if hp[i] is not None and hp[i + 1] is not None]
        if drops and max(drops)[0] > 0:
            key_steps.setdefault(max(drops)[1], f"before the biggest HP drop in combat {f.index} (-{max(drops)[0]})")
    for s in steps[-3:]:
        key_steps.setdefault(s["step"], "end of run")
    out += ["", "Your thoughts at key moments:"]
    for number in sorted(key_steps):
        s = by_number[number]
        done = "; ".join(_describe(a) for a in s.get("executed") or []) or "no action"
        out.append(f"- step {number} ({key_steps[number]}): {_clip(s.get('thought')) or '(none)'} -> {done}")

    shown_counts: dict[str, int] = {}
    for s in steps:
        for note_id in s.get("notes_shown") or []:
            shown_counts[note_id] = shown_counts.get(note_id, 0) + 1
    out += ["", "Notes shown during this run:"]
    shown_notes = [store.get(i) for i in shown_counts if store.get(i)]
    out += [_note_line(n, f"; shown on {shown_counts[n.id]} steps this run") for n in shown_notes] or ["(none)"]

    contexts = [context_from_state(s.get("state")) for s in steps if s.get("state")]
    nearby = [n for n in store.live() if n.id not in shown_counts and any(matches(n, c) for c in contexts)]
    out += ["", "Other existing notes that apply to screens in this run (check before adding):"]
    out += [_note_line(n) for n in nearby[:MAX_NEARBY_NOTES]] or ["(none)"]
    return "\n".join(out)


def build_request(store: NoteStore, run: Run, model: str = DEFAULT_MODEL) -> dict:
    return {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": digest(store, run)}],
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": OPS_SCHEMA}},
    }


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def find_duplicate(store: NoteStore, keys: dict, text: str):
    """A live note with the same keys and similar text, or None."""
    target = _normalize(text)
    want = {k: _normalize(v) for k, v in keys.items()}
    for note in store.live():
        if {k: _normalize(v) for k, v in note.keys.items()} != want:
            continue
        if SequenceMatcher(None, _normalize(note.text), target).ratio() >= SIMILARITY:
            return note
    return None


def apply_ops(store: NoteStore, ops: list[dict], run_id: str) -> list[dict]:
    """Apply validated ops to the store in memory. Returns one result per op."""
    results, adds = [], 0
    for op in ops:
        kind, note_id = op.get("op"), op.get("id") or ""
        text = clip_text(op.get("text") or "")
        result = {"op": kind, "id": note_id, "reason": op.get("reason", "")}
        if kind == "ADD":
            keys = clean_keys(op.get("keys"))
            duplicate = find_duplicate(store, keys, text) if text else None
            if op.get("kind") not in KINDS or not text:
                result["result"] = "skipped: needs a valid kind and text"
            elif adds >= MAX_ADDS:
                result["result"] = f"skipped: at most {MAX_ADDS} ADDs per run"
            elif duplicate:
                result["result"] = f"skipped: duplicate of {duplicate.id}"
            else:
                note = store.add(op["kind"], text, keys, run_id)
                adds += 1
                result.update(id=note.id, result="added")
            results.append(result)
            continue

        note = store.get(note_id)
        if note is None or note.status == "retired":
            result["result"] = "skipped: unknown or retired id"
        elif kind == "UPDATE":
            if not text:
                result["result"] = "skipped: UPDATE needs text"
            else:
                note.text = text
                keys = clean_keys(op.get("keys"))
                if keys:
                    note.keys = keys
                if op.get("kind") in KINDS:
                    note.kind = op["kind"]
                result["result"] = "updated"
        elif kind == "DOWNVOTE":
            note.downvotes += 1
            if note.downvotes >= DOWNVOTES_TO_RETIRE:
                note.status = "retired"
            elif note.status == "active":
                note.status = "probation"  # back to A/B testing
            result["result"] = f"downvoted ({note.downvotes}), now {note.status}"
        elif kind == "RETIRE":
            note.status = "retired"
            result["result"] = "retired"
        else:
            result["result"] = "skipped: unknown op"
        if note is not None and result["result"].startswith(("updated", "downvoted", "retired")):
            note.updated = now()
            if run_id not in note.source_runs:
                note.source_runs.append(run_id)
        results.append(result)
    return results


def reflect(store: NoteStore, run: Run, client=None, model: str = DEFAULT_MODEL) -> dict:
    """Score the run, ask Claude for delta ops, apply them, save the store, log the call. Skips reset runs."""
    if run.meta.get("kind") == "reset":
        return {"skipped": "reset run", "results": []}
    score = score_run(store, run)
    store.save()  # keep the scores even if the call below fails; memory_score.json marks the run scored
    request = build_request(store, run, model)
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    response = client.messages.create(**request)

    u = response.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    usage.cost_usd = pricing.cost_usd(model, usage)
    record = {
        "model": model,
        "time": now(),
        "stop_reason": response.stop_reason,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        },
        "cost_usd": usage.cost_usd,
        "scored": "skipped" not in score,
    }
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        ops = json.loads(text)["ops"] if response.stop_reason not in ("refusal", "max_tokens") else []
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        ops = []
        record["error"] = f"unparseable reply: {exc!r}"
    if response.stop_reason in ("refusal", "max_tokens"):
        record["error"] = f"stop_reason {response.stop_reason}"
    record["ops"] = ops
    record["results"] = apply_ops(store, ops, run.id)
    store.save()
    (run.dir / REFLECT_FILE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record
