"""The pure-Python tests in sts_cua.stats against textbook values and brute force.

Reference values: the scipy documentation's examples (Darwin's Zea mays differences for
wilcoxon, its male/female example for mannwhitneyu), Fisher's lady tasting tea, and values
cross-checked with scipy 1.18.1 (wilcoxon(method="approx", correction=True),
mannwhitneyu(method="asymptotic")).
"""

import itertools
import math

import pytest

from sts_cua.stats import fisher_exact, mann_whitney, mcnemar_exact, rankdata, wilcoxon

# Darwin's cross- vs self-fertilized Zea mays height differences (eighths of an inch).
DARWIN = [6, 8, 14, 16, 23, 24, 28, 29, 41, -48, 49, 56, 60, -67, 75]
MALES, FEMALES = [19, 22, 16, 29, 24], [20, 11, 17, 12]


def test_rankdata_averages_ties():
    assert rankdata([10, 20, 20, 5, 20]) == [2.0, 4.0, 4.0, 1.0, 4.0]
    assert rankdata([]) == []


def test_wilcoxon_textbook_exact():
    r = wilcoxon([0] * len(DARWIN), DARWIN)
    assert r.method == "exact" and r.n == 15 and r.statistic == 24.0
    assert r.p_value == pytest.approx(0.041259765625, abs=1e-12)
    assert r.effect_size == pytest.approx((96 - 24) / 120)  # W+ = 96, W- = 24


def test_wilcoxon_normal_approximation():
    r = wilcoxon([0] * len(DARWIN), DARWIN, method="normal")
    assert r.p_value == pytest.approx(0.043772323763041174, rel=1e-9)
    # With ties and a zero difference (dropped): scipy approx with continuity correction.
    a, b = [10, 12, 12, 15, 20, 20, 20, 31], [12, 15, 15, 22, 20, 25, 33, 100]
    r = wilcoxon(a, b, method="normal")
    assert r.n == 7 and r.statistic == 0 and r.p_value == pytest.approx(0.02225412482309912, rel=1e-9)


def test_wilcoxon_direction_and_degenerate_cases():
    assert wilcoxon([1, 2, 3], [1, 2, 3]).p_value == 1.0  # all differences zero
    assert wilcoxon([5, 5, 5], [1, 2, 3]).effect_size == -1.0
    with pytest.raises(ValueError):
        wilcoxon([1, 2], [1])


def brute_wilcoxon(diffs):
    """Two-sided exact p by flipping every sign: twice the smaller tail of W+."""
    ranks = rankdata([abs(d) for d in diffs])
    observed = sum(r for r, d in zip(ranks, diffs) if d > 0)
    totals = [sum(r for r, s in zip(ranks, signs) if s) for signs in itertools.product((0, 1), repeat=len(diffs))]
    low = sum(t <= observed + 1e-9 for t in totals) / len(totals)
    high = sum(t >= observed - 1e-9 for t in totals) / len(totals)
    return min(1.0, 2 * min(low, high))


@pytest.mark.parametrize("diffs", [[3, 3, -1, 5, 5, 5, -2, 8], [1, -1, 2, 2, -2, 4], [17, 17, 17, -17, 4]])
def test_wilcoxon_exact_with_ties_matches_brute_force(diffs):
    assert wilcoxon([0] * len(diffs), diffs).p_value == pytest.approx(brute_wilcoxon(diffs))


def test_mann_whitney_textbook():
    r = mann_whitney(FEMALES, MALES)  # U of the males
    assert r.method == "exact" and r.statistic == 17.0
    assert r.p_value == pytest.approx(1 / 9)
    assert r.effect_size == pytest.approx(2 * 17 / 20 - 1)
    r = mann_whitney(FEMALES, MALES, method="normal")
    assert r.p_value == pytest.approx(0.11134688653314041, rel=1e-9)
    # Ties: scipy asymptotic with continuity and tie correction.
    x, y = [3, 5, 5, 7, 9, 9, 12], [5, 8, 9, 9, 14, 16, 20, 21]
    r = mann_whitney(x, y, method="normal")
    assert r.statistic == 44.0 and r.p_value == pytest.approx(0.06927409004966426, rel=1e-9)


def brute_mann_whitney(a, b):
    ranks = rankdata(list(b) + list(a))
    observed = sum(ranks[: len(b)])
    sums = [sum(ranks[i] for i in combo) for combo in itertools.combinations(range(len(ranks)), len(b))]
    low = sum(s <= observed + 1e-9 for s in sums) / len(sums)
    high = sum(s >= observed - 1e-9 for s in sums) / len(sums)
    return min(1.0, 2 * min(low, high))


@pytest.mark.parametrize("a,b", [([3, 5, 5, 7, 9, 9, 12], [5, 8, 9, 9, 14, 16, 20, 21]),
                                 ([1, 1, 1, 2], [1, 2, 2, 3, 3]), ([10, 20], [30, 40, 50])])
def test_mann_whitney_exact_with_ties_matches_brute_force(a, b):
    assert mann_whitney(a, b).p_value == pytest.approx(brute_mann_whitney(a, b))


def test_mann_whitney_needs_both_samples():
    with pytest.raises(ValueError):
        mann_whitney([], [1.0])
    assert mann_whitney([1, 2, 3], [1, 2, 3]).effect_size == 0.0


def test_fisher_exact():
    assert fisher_exact([[3, 1], [1, 3]]) == pytest.approx(0.4857142857142857)  # lady tasting tea
    # AgenticSTS at A0: 6/10 wins with its skill layer vs 3/10 without (docs/learning.md: p ~ 0.37).
    assert fisher_exact([[6, 4], [3, 7]]) == pytest.approx(0.3698499642772088)
    assert fisher_exact([[0, 0], [0, 0]]) == 1.0
    assert fisher_exact([[5, 0], [0, 5]]) == pytest.approx(2 / math.comb(10, 5))


def test_mcnemar_exact():
    assert mcnemar_exact(2, 8) == pytest.approx(0.109375)  # 2 * P(X <= 2), X ~ Bin(10, 1/2)
    assert mcnemar_exact(0, 0) == 1.0 and mcnemar_exact(3, 3) == 1.0
