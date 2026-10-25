"""Per-token prices in USD per million tokens.

Source: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-25).
Haiku 5.5 prices are for prompts up to 100,000 tokens, which every step here stays under.
"""

from __future__ import annotations

from .models.base import Usage

PRICES = {
    # model: (input, output, 5m cache write, cache read)
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
}


def cost_usd(model: str, usage: Usage) -> float:
    if model not in PRICES:
        return 0.0
    inp, out, write, read = PRICES[model]
    return (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_creation_input_tokens * write
        + usage.cache_read_input_tokens * read
    ) / 1_000_000
