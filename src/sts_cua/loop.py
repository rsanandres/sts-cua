"""The agent loop: screenshot -> model -> actions -> log, until a stop condition."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageChops, ImageStat

from .actions import Wait
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
    max_unchanged: int = 8  # stop after this many acting steps in a row that didn't change the screen
    unchanged_threshold: float = 1.0  # mean per-pixel difference (0-255) below which the screen counts as unchanged


def screen_diff(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute pixel difference on a small grayscale copy, 0 (same) to 255."""
    size = (160, 90)
    small_a = a.convert("L").resize(size)
    small_b = b.convert("L").resize(size)
    return ImageStat.Stat(ImageChops.difference(small_a, small_b)).mean[0]


def run(
    model: Model,
    screen: Screen,
    log: RunLog,
    config: LoopConfig | None = None,
    state_fn: Callable[[], dict | None] | None = None,
    stop_fn: Callable[[dict | None], str | None] | None = None,
    notes_fn: Callable[[dict | None], tuple[str, list[str]]] | None = None,
) -> dict:
    """state_fn, if given, returns the true game state each step. It is logged, never shown to the model.

    stop_fn, if given, is called with each step's state before the model is asked; a string
    return logs the step with no decision and ends the run with that string as end_reason.

    notes_fn, if given, maps that state to (notes text, note ids): lessons from earlier runs
    shown to the model this step (sts_cua.memory.Retriever). The ids are logged as notes_shown.
    """
    config = config or LoopConfig()
    history: list[HistoryEntry] = []
    idle = errors = unchanged = 0
    previous: Image.Image | None = None
    end_reason = "max_steps"

    for step in range(1, config.max_steps + 1):
        screenshot = screen.capture()
        if previous is not None and history and any(not isinstance(a, Wait) for a in history[-1].actions):
            if screen_diff(previous, screenshot) < config.unchanged_threshold:
                history[-1].unchanged = True
                unchanged += 1
                if unchanged >= config.max_unchanged:
                    log.step(step, screenshot, None, [], error="stuck: screen unchanged", state=state_fn() if state_fn else None)
                    end_reason = "stuck"
                    break
            else:
                unchanged = 0
        previous = screenshot
        state = state_fn() if state_fn else None
        reason = stop_fn(state) if stop_fn else None
        if reason:
            log.step(step, screenshot, None, [], state=state)
            end_reason = reason
            break
        notes, notes_shown = notes_fn(state) if notes_fn else ("", None)
        try:
            decision = model.decide(Observation(step, screenshot, history, notes=notes))
        except Exception as exc:  # API or adapter failure: log it and keep going, up to max_errors
            log.step(step, screenshot, None, [], error=f"model: {exc!r}", state=state, notes_shown=notes_shown)
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

        log.step(step, screenshot, decision, executed, error, state=state, notes_shown=notes_shown)
        history.append(HistoryEntry(step, decision.thought, executed))

        if decision.done:
            end_reason = "done"
            break
        errors = errors + 1 if error else 0
        # Waiting doesn't count as progress: a model that only waits is stuck or finished.
        idle = 0 if any(not isinstance(a, Wait) for a in executed) else idle + 1
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
