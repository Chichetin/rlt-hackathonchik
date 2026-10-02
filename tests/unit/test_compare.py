"""Paired comparison must not silently discard or invent observations."""

import pytest

from src.eval.compare import paired


def test_duplicate_baseline_query_is_rejected():
    a = {"exp_id": "A", "per_query": [{"query_id": "q1", "recall@10": 1.0}]}
    b = {"exp_id": "B", "per_query": [
        {"query_id": "q1", "recall@10": 0.0},
        {"query_id": "q1", "recall@10": 1.0},
    ]}

    with pytest.raises(ValueError, match="парное сравнение невозможно"):
        paired(a, b)


@pytest.mark.parametrize(("rows_a", "rows_b"), [
    ([], []),
    ([], [{"query_id": "q1", "recall@10": 1.0}]),
    ([{"query_id": "q1", "recall@10": 1.0}], []),
])
def test_empty_run_is_rejected(rows_a, rows_b):
    a = {"exp_id": "A", "per_query": rows_a}
    b = {"exp_id": "B", "per_query": rows_b}

    with pytest.raises(ValueError, match="нет кейсов.*парное сравнение невозможно"):
        paired(a, b)
