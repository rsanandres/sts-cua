import pytest

from sts_cua.models.base import Usage
from sts_cua.pricing import cost_usd


@pytest.mark.parametrize("model, expected", [
    ("claude-haiku-5-5", 0.10 + 0.50 + 0.125 + 0.01),
    ("claude-sonnet-5-5", 2.00 + 10.00 + 2.50 + 0.10),
    ("claude-opus-5-5", 4.00 + 20.00 + 5.00 + 0.20),
])
def test_cost_per_million_of_each_kind(model, expected):
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000,
                  cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert cost_usd(model, usage) == pytest.approx(expected)


def test_unknown_model_costs_nothing():
    assert cost_usd("qwen3-vl-8b", Usage(input_tokens=1000)) == 0.0
