"""The interface every model adapter implements.

The harness only calls `Model.decide`. Each adapter converts its own coordinate
convention to screen pixels, so the loop never changes when the model does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image

from ..actions import Action


@dataclass
class HistoryEntry:
    step: int
    thought: str
    actions: list[Action]
    # Set after the next screenshot: the step's actions left the screen (almost) unchanged.
    unchanged: bool = False


@dataclass
class Observation:
    step: int
    screenshot: Image.Image
    history: list[HistoryEntry]
    # Lessons from earlier runs picked for this step (sts_cua.memory); empty when memory is off.
    notes: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Decision:
    thought: str
    actions: list[Action]
    # The model's internal reasoning when the provider returns it (Claude returns a summary).
    reasoning: str = ""
    stop_reason: str | None = None
    done: bool = False  # the model says the goal is reached
    usage: Usage = field(default_factory=Usage)
    # Provider response, kept for the run log.
    raw: dict | None = None
    # Who decided: "model", or "rule" for a co-op general rule (sts_cua.rules) taken with no model call.
    source: str = "model"
    rule: str | None = None  # the rule that acted, or whose directive the model followed


class Model(Protocol):
    name: str

    def decide(self, obs: Observation) -> Decision: ...


class ScriptedModel:
    """Replays a fixed list of action batches. For harness tests with no model."""

    name = "scripted"

    def __init__(self, script: list[list[Action]], thought: str = "scripted"):
        self._script = script
        self._thought = thought

    def decide(self, obs: Observation) -> Decision:
        index = obs.step - 1
        actions = self._script[index] if index < len(self._script) else []
        return Decision(thought=self._thought, actions=list(actions), stop_reason="scripted")
