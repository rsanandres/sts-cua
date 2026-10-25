"""The agent loop: screenshot -> model -> actions -> log, until a stop condition."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .models.base import HistoryEntry, Model, Observation
from .runlog import RunLog
from .screen import Screen


@dataclass
class LoopConfig:
    max_steps: int = 50
    max_cost_usd: float | None = None
    settle_seconds: float = 1.0  # let animations finish before the next screenshot
    max_idle_steps: int = 3  # stop after this many steps in a row with no action
    max_errors: int = 3  # stop after this many model or input errors in a row


def run(model: Model, screen: Screen, log: RunLog, config: LoopConfig | None = None) -> dict:
    config = config or LoopConfig()
    history: list[HistoryEntry] = []
    idle = errors = 0
    end_reason = "max_steps"

    for step in range(1, config.max_steps + 1):
        screenshot = screen.capture()
        try:
            decision = model.decide(Observation(step, screenshot, history))
        except Exception as exc:  # API or adapter failure: log it and keep going, up to max_errors
            log.step(step, screenshot, None, [], error=f"model: {exc!r}")
            errors += 1
            if errors >= config.max_errors:
                end_reason = "model_errors"
                break
            continue

        executed, error = [], None
        for action in decision.actions:
            try:
                screen.perform(action)
            except Exception as exc:
                error = f"action {action!r}: {exc!r}"
                break
            executed.append(action)

        log.step(step, screenshot, decision, executed, error)
        history.append(HistoryEntry(step, decision.thought, executed))

        errors = errors + 1 if error else 0
        idle = 0 if executed else idle + 1
        if errors >= config.max_errors:
            end_reason = "action_errors"
            break
        if idle >= config.max_idle_steps:
            end_reason = "idle"
            break
        if config.max_cost_usd is not None and log.cost_usd >= config.max_cost_usd:
            end_reason = "max_cost"
            break
        if config.settle_seconds:
            time.sleep(config.settle_seconds)

    return log.close(end_reason)
