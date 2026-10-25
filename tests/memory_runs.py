"""Synthetic run directories for the memory tests, shaped like RunLog output."""

import json
from pathlib import Path

from anthropic.types import Message

CLICK = {"kind": "click", "x": 640, "y": 300, "button": "left", "count": 1, "modifiers": None}


def combat(enemy, hp, floor=1, act=1, hand=("Strike", "Defend"), max_hp=80):
    return {
        "state_type": "monster", "act": act, "floor": floor,
        "player": {"hp": hp, "max_hp": max_hp, "block": 0, "energy": 3, "gold": 99},
        "hand": [{"slot": i + 1, "name": name, "cost": "1", "target": "AnyEnemy"} for i, name in enumerate(hand)],
        "round": 1,
        "enemies": [{"name": enemy, "hp": 20, "max_hp": 43, "block": 0, "intents": ["12"]}],
    }


def screen(state_type, hp=80, floor=1, act=1, max_hp=80):
    return {"state_type": state_type, "act": act, "floor": floor,
            "player": {"hp": hp, "max_hp": max_hp, "block": 0, "energy": 3, "gold": 99}, "hand": []}


def step(state, shown=None, error=None, thought="thinking", executed=None):
    record = {"state": state, "thought": thought, "executed": [CLICK] if executed is None else executed}
    if shown is not None:
        record["notes_shown"] = shown
    if error:
        record["error"] = error
    return record


def fight(enemy, floor, hp_start, hp_lost, shown=None, steps=3):
    """A combat of `steps` steps losing hp_lost HP, then the rewards screen."""
    out = []
    for i in range(steps):
        hp = hp_start - round(hp_lost * i / (steps - 1)) if steps > 1 else hp_start - hp_lost
        out.append(step(combat(enemy, hp, floor=floor), shown=shown))
    out.append(step(screen("rewards", hp=hp_start - hp_lost, floor=floor), shown=[] if shown is not None else None))
    return out


def write_run(root, name, steps, meta=None, summary=None) -> Path:
    run_dir = Path(root) / name
    run_dir.mkdir(parents=True)
    meta = {"model": "test", "game_build": "unknown", "started_at": "2026-10-25T12:00:00+00:00",
            "memory": {"path": str(Path(root) / "notes.jsonl"), "seed": 1}, **(meta or {})}
    (run_dir / "meta.json").write_text(json.dumps(meta))
    (run_dir / "summary.json").write_text(json.dumps(summary or {"end_reason": "max_steps"}))
    lines = []
    for number, record in enumerate(steps, 1):
        lines.append(json.dumps({"step": number, "screenshot": f"screenshots/step_{number:04d}.png", **record}))
    (run_dir / "steps.jsonl").write_text("\n".join(lines) + "\n")
    return run_dir


class FakeClient:
    """Stands in for anthropic.Anthropic(): every messages.create returns {"ops": ops}."""

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
