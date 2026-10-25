"""The agent loop: screenshot -> model -> actions -> log, until a stop condition."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageChops, ImageStat

from .actions import Wait
from .models.base import Decision, HistoryEntry, Model, Observation
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
    # Stop after this many seconds without an action that changed the screen (co-op; None: no limit).
    max_idle_seconds: float | None = None
    poll_seconds: float = 0.5  # how often a rule's wait (hold) re-reads the mod state


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
    waiting_fn: Callable[[dict | None], str | None] | None = None,
    rules_fn: Callable[[dict | None], object | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """state_fn, if given, returns the true game state each step. It is logged, never shown to the model.

    stop_fn, if given, is called with each step's state before the model is asked; a string
    return logs the step with no decision and ends the run with that string as end_reason.

    notes_fn, if given, maps that state to (notes text, note ids): lessons from earlier runs
    shown to the model this step (sts_cua.memory.Retriever). The ids are logged as notes_shown.

    waiting_fn, if given, maps that state to why waiting is normal right now (co-op: the
    teammate's turn, a pending vote; sts_cua.coop.waiting), or None. While it gives a reason,
    wait-only steps don't count as idle and unchanged screens don't count as stuck; the reason
    is logged as `waiting`. config.max_idle_seconds still ends a run that makes no progress.

    rules_fn, if given, maps that state to an sts_cua.rules.Ruling, or None, before the model
    is asked. A ruling with actions is the step's decision (source "rule", no model call); a
    hold ruling's wait ends early once waiting_fn's reason changes, polling every
    config.poll_seconds. A ruling with a directive puts that line in front of the step's notes.
    """
    config = config or LoopConfig()
    history: list[HistoryEntry] = []
    idle = errors = unchanged = 0
    previous: Image.Image | None = None
    end_reason = "max_steps"
    last_progress = clock()  # when an action of the agent's last changed the screen

    try:
        for step in range(1, config.max_steps + 1):
            screenshot = screen.capture()
            state = state_fn() if state_fn else None
            waiting = waiting_fn(state) if waiting_fn else None
            if previous is not None and history and any(not isinstance(a, Wait) for a in history[-1].actions):
                if screen_diff(previous, screenshot) < config.unchanged_threshold:
                    history[-1].unchanged = True
                    if not waiting:  # clicks that change nothing are normal while the teammate plays
                        unchanged += 1
                    if unchanged >= config.max_unchanged:
                        log.step(step, screenshot, None, [], error="stuck: screen unchanged", state=state, waiting=waiting)
                        end_reason = "stuck"
                        break
                else:
                    unchanged = 0
                    last_progress = clock()
            previous = screenshot
            if config.max_idle_seconds is not None and clock() - last_progress >= config.max_idle_seconds:
                log.step(step, screenshot, None, [], error="idle timeout: no progress", state=state, waiting=waiting)
                end_reason = "idle_timeout"
                break
            reason = stop_fn(state) if stop_fn else None
            if reason:
                log.step(step, screenshot, None, [], state=state, waiting=waiting)
                end_reason = reason
                break
            notes, notes_shown = notes_fn(state) if notes_fn else ("", None)
            ruling = rules_fn(state) if rules_fn else None
            if ruling is not None and ruling.directive:
                notes = f"{ruling.directive}\n\n{notes}" if notes else ruling.directive
            try:
                if ruling is not None and ruling.actions:
                    decision = Decision(thought=f"[rule {ruling.rule}] {ruling.why}", actions=list(ruling.actions),
                                        stop_reason="rule", source="rule", rule=ruling.rule)
                else:
                    decision = model.decide(Observation(step, screenshot, history, notes=notes))
                    if ruling is not None:
                        decision.rule = ruling.rule
            except Exception as exc:  # API or adapter failure: log it and keep going, up to max_errors
                log.step(step, screenshot, None, [], error=f"model: {exc!r}", state=state, notes_shown=notes_shown,
                         waiting=waiting)
                errors += 1
                if errors >= config.max_errors:
                    end_reason = "model_errors"
                    break
                continue

            executed, error = [], None
            for action in decision.actions:
                if ruling is not None and ruling.hold and isinstance(action, Wait) and state_fn and waiting_fn:
                    executed.append(_hold(action.seconds, waiting, state_fn, waiting_fn, config.poll_seconds, clock, sleep))
                    continue
                try:
                    screen.perform(action)
                except Exception as exc:
                    error = f"action {action!r}: {exc!r}"
                    break
                executed.append(action)

            log.step(step, screenshot, decision, executed, error, state=state, notes_shown=notes_shown, waiting=waiting)
            history.append(HistoryEntry(step, decision.thought, executed))

            if decision.done:
                end_reason = "done"
                break
            errors = errors + 1 if error else 0
            # Waiting doesn't count as progress: a model that only waits is stuck or finished,
            # unless waiting_fn says waiting is expected (co-op: the teammate's move).
            if any(not isinstance(a, Wait) for a in executed):
                idle = 0
            elif not waiting:
                idle += 1
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
                sleep(config.settle_seconds)
    except KeyboardInterrupt:
        # Ctrl+C or the app's Stop: still write summary.json so the run's spend counts toward the budget.
        log.close("interrupted")
        raise

    return log.close(end_reason)


def _hold(seconds, reason, state_fn, waiting_fn, poll, clock, sleep) -> Wait:
    """Wait up to `seconds`, re-reading the state every `poll` seconds; stop once the wait reason changes."""
    start = clock()
    while (elapsed := clock() - start) < seconds:
        sleep(min(poll, seconds - elapsed))
        if waiting_fn(state_fn()) != reason:
            break
    return Wait(round(clock() - start, 2))
