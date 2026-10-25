"""Two-sided rank tests and win-rate tests in pure Python (no scipy), for sts_cua.eval.

- wilcoxon(a, b): paired Wilcoxon signed-rank test on b - a (zero differences dropped).
- mann_whitney(a, b): unpaired Mann-Whitney U test of b against a.
- fisher_exact(table): Fisher's exact test on a 2x2 table (unpaired win rates).
- mcnemar_exact(b, c): exact McNemar test on the discordant pair counts (paired win rates).

p-values are exact when the samples are small enough to enumerate (method "exact"); with
ties the exact test is conditional on the observed ranks, which is still a valid
permutation test. Otherwise a normal approximation with tie correction and a 0.5
continuity correction (method "normal"). Effect sizes are rank-biserial correlations:
+1 when every b beats a, -1 when every a beats b.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

EXACT_WILCOXON_MAX_N = 50  # pairs; the DP is over 2^n sign patterns folded into rank sums
EXACT_MANN_WHITNEY_MAX_N = 60  # n1 + n2


@dataclass
class TestResult:
    test: str
    n: int  # pairs used (Wilcoxon) or total runs (Mann-Whitney)
    statistic: float  # Wilcoxon: min(W+, W-), as scipy reports it; Mann-Whitney: U of b
    p_value: float
    method: str  # exact or normal
    effect_size: float | None  # rank-biserial correlation, b relative to a
    effect_size_name: str = "rank_biserial"
    z: float | None = None  # normal method only

    def to_dict(self) -> dict:
        return asdict(self)


def rankdata(values: list[float]) -> list[float]:
    """1-based ranks, ties getting the average of the ranks they span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def _tie_sizes(values: list[float]) -> list[int]:
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return [t for t in counts.values() if t > 1]


def normal_two_sided(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def _two_sided(dist: dict[int, int], total: int, observed: int) -> float:
    """Two-sided p from a counted distribution: twice the smaller tail, capped at 1."""
    low = sum(c for s, c in dist.items() if s <= observed)
    high = sum(c for s, c in dist.items() if s >= observed)
    return min(1.0, 2 * min(low, high) / total)


def _normal(stat: float, mean: float, var: float, continuity: bool = True) -> tuple[float, float]:
    if var <= 0:
        return 0.0, 1.0
    diff = abs(stat - mean)
    if continuity:
        diff = max(0.0, diff - 0.5)
    z = math.copysign(diff / math.sqrt(var), stat - mean)
    return z, normal_two_sided(z)


def wilcoxon(a: list[float], b: list[float], method: str = "auto") -> TestResult:
    """Paired signed-rank test of b - a. method: auto (exact up to EXACT_WILCOXON_MAX_N pairs), exact or normal."""
    if len(a) != len(b):
        raise ValueError("wilcoxon needs paired samples of equal length")
    diffs = [y - x for x, y in zip(a, b) if y - x != 0]
    n = len(diffs)
    if n == 0:
        return TestResult("wilcoxon_signed_rank", 0, 0.0, 1.0, "exact", None)
    ranks = rankdata([abs(d) for d in diffs])
    w_plus = sum(r for r, d in zip(ranks, diffs) if d > 0)
    w_minus = sum(r for r, d in zip(ranks, diffs) if d < 0)
    effect = (w_plus - w_minus) / (w_plus + w_minus)
    if method == "auto":
        method = "exact" if n <= EXACT_WILCOXON_MAX_N else "normal"

    if method == "exact":
        # Under H0 each sign is a fair coin: count sign patterns by 2 * W+ (doubled ranks are integers).
        dist = {0: 1}
        for r in ranks:
            step = round(2 * r)
            nxt = dict(dist)
            for s, c in dist.items():
                nxt[s + step] = nxt.get(s + step, 0) + c
            dist = nxt
        p = _two_sided(dist, 2**n, round(2 * w_plus))
        return TestResult("wilcoxon_signed_rank", n, float(min(w_plus, w_minus)), p, "exact", effect)
    if method != "normal":
        raise ValueError("method must be auto, exact or normal")
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - sum(t**3 - t for t in _tie_sizes([abs(d) for d in diffs])) / 48
    z, p = _normal(w_plus, mean, var)
    return TestResult("wilcoxon_signed_rank", n, float(min(w_plus, w_minus)), p, "normal", effect, z=z)


def mann_whitney(a: list[float], b: list[float], method: str = "auto") -> TestResult:
    """Unpaired U test of b against a. method: auto (exact up to EXACT_MANN_WHITNEY_MAX_N runs), exact or normal."""
    n1, n2 = len(b), len(a)
    if n1 == 0 or n2 == 0:
        raise ValueError("mann_whitney needs two non-empty samples")
    pooled = list(b) + list(a)
    ranks = rankdata(pooled)
    rank_sum = sum(ranks[:n1])
    u = rank_sum - n1 * (n1 + 1) / 2  # pairs where b beats a, ties counting a half
    effect = 2 * u / (n1 * n2) - 1
    total_n = n1 + n2
    if method == "auto":
        method = "exact" if total_n <= EXACT_MANN_WHITNEY_MAX_N else "normal"

    if method == "exact":
        # Count size-n1 subsets of the pooled doubled ranks by their sum: dist[k][s].
        dist: list[dict[int, int]] = [{0: 1}] + [{} for _ in range(n1)]
        for r in ranks:
            step = round(2 * r)
            for k in range(min(n1, total_n) - 1, -1, -1):
                for s, c in dist[k].items():
                    dist[k + 1][s + step] = dist[k + 1].get(s + step, 0) + c
        p = _two_sided(dist[n1], math.comb(total_n, n1), round(2 * rank_sum))
        return TestResult("mann_whitney_u", total_n, u, p, "exact", effect)
    if method != "normal":
        raise ValueError("method must be auto, exact or normal")
    ties = sum(t**3 - t for t in _tie_sizes(pooled))
    var = n1 * n2 / 12 * ((total_n + 1) - ties / (total_n * (total_n - 1)))
    z, p = _normal(u, n1 * n2 / 2, var)
    return TestResult("mann_whitney_u", total_n, u, p, "normal", effect, z=z)


def _hypergeom(k: int, row1: int, col1: int, total: int) -> float:
    return math.comb(col1, k) * math.comb(total - col1, row1 - k) / math.comb(total, row1)


def fisher_exact(table: list[list[int]]) -> float:
    """Two-sided p of Fisher's exact test on [[a, b], [c, d]]: tables no more likely than the observed one."""
    (a, b), (c, d) = table
    row1, col1, total = a + b, a + c, a + b + c + d
    if total == 0:
        return 1.0
    observed = _hypergeom(a, row1, col1, total)
    lo, hi = max(0, row1 + col1 - total), min(row1, col1)
    p = sum(q for k in range(lo, hi + 1) if (q := _hypergeom(k, row1, col1, total)) <= observed * (1 + 1e-7))
    return min(1.0, p)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p: discordant pairs b (only the first arm won) vs c (only the second won)."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)
