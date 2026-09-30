"""Ranking metrics and bootstrap intervals over queries (research/EVAL.md, section 7)."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np

BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 20261001


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for i, x in enumerate(ranked, start=1):
        if x in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, x in enumerate(ranked[:k], start=1) if x in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


METRICS: dict[str, Callable[[Sequence[str], set[str]], float]] = {
    "recall@10": lambda r, rel: recall_at_k(r, rel, 10),
    "mrr": reciprocal_rank,
    "ndcg@10": lambda r, rel: ndcg_at_k(r, rel, 10),
}


def bootstrap_ci(values: Sequence[float], n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED,
                 alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap over queries. Fewer than 2 queries -> the point value twice."""
    arr = np.asarray(values, dtype=float)
    if arr.size < 2:
        v = float(arr.mean()) if arr.size else 0.0
        return v, v
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, arr.size, size=(n, arr.size))].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))
