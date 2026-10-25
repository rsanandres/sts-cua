"""Writes one directory per run: meta.json, steps.jsonl, a screenshot per step, summary.json."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .actions import Action, action_to_dict
from .models.base import Decision


class RunLog:
    def __init__(self, runs_dir: str | Path, meta: dict, keep_raw: bool = True):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = Path(runs_dir) / stamp
        suffix = 1
        while self.dir.exists():
            self.dir = Path(runs_dir) / f"{stamp}-{suffix}"
            suffix += 1
        (self.dir / "screenshots").mkdir(parents=True)
        self._keep_raw = keep_raw
        self._steps = (self.dir / "steps.jsonl").open("a", encoding="utf-8")
        self.steps = 0
        self.cost_usd = 0.0
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._started = datetime.now(timezone.utc)
        self._write_json("meta.json", {**meta, "started_at": self._started.isoformat()})

    def step(
        self,
        step: int,
        screenshot: Image.Image,
        decision: Decision | None,
        executed: list[Action],
        error: str | None = None,
    ) -> None:
        shot_name = f"screenshots/step_{step:04d}.png"
        screenshot.save(self.dir / shot_name)
        record = {"step": step, "time": datetime.now(timezone.utc).isoformat(), "screenshot": shot_name}
        if decision is not None:
            u = decision.usage
            record.update(
                thought=decision.thought,
                reasoning=decision.reasoning,
                stop_reason=decision.stop_reason,
                proposed=[action_to_dict(a) for a in decision.actions],
                usage={
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_input_tokens": u.cache_read_input_tokens,
                    "cache_creation_input_tokens": u.cache_creation_input_tokens,
                    "cost_usd": u.cost_usd,
                },
            )
            if self._keep_raw and decision.raw is not None:
                record["raw"] = decision.raw
            self.cost_usd += u.cost_usd
            self.tokens["input"] += u.input_tokens
            self.tokens["output"] += u.output_tokens
            self.tokens["cache_read"] += u.cache_read_input_tokens
            self.tokens["cache_write"] += u.cache_creation_input_tokens
        record["executed"] = [action_to_dict(a) for a in executed]
        if error:
            record["error"] = error
        self._steps.write(json.dumps(record) + "\n")
        self._steps.flush()
        self.steps = step

    def close(self, end_reason: str) -> dict:
        self._steps.close()
        ended = datetime.now(timezone.utc)
        summary = {
            "end_reason": end_reason,
            "steps": self.steps,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "ended_at": ended.isoformat(),
            "seconds": round((ended - self._started).total_seconds(), 1),
        }
        self._write_json("summary.json", summary)
        return summary

    def _write_json(self, name: str, data: dict) -> None:
        (self.dir / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
