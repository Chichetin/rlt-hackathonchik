"""#280: winner@k and winner MRR — the metrics other teams report (the winner of the lot in the top-k)."""

from __future__ import annotations

from datetime import date

import pytest

from src.contracts import Query
from src.eval import compare as C
from src.eval import run as R
from src.eval.metrics import WINNER_METRICS, winner_at_k, winner_reciprocal_rank
from src.eval.split import EvalCase


def test_winner_hit_and_mrr():
    ranked = ["a", "b", "c"]
    assert [winner_at_k(ranked, {"c"}, k) for k in (1, 2, 3)] == [0.0, 0.0, 1.0]
    assert winner_at_k(ranked, {"x", "b"}, 2) == 1.0  # one of several winners is enough
    assert winner_reciprocal_rank(ranked, {"c"}) == 1 / 3
    assert winner_reciprocal_rank(ranked, {"zzz"}) == 0.0


def _case(qid: str, grades: dict[str, int]) -> EvalCase:
    return EvalCase(query=Query(query_id=qid, text=qid, channel="ЭМ"), relevant_inns=list(grades),
                    date=date(2025, 7, 10), grades=grades)


def test_evaluate_counts_only_cases_with_a_winner(monkeypatch):
    monkeypatch.setattr(R, "_rank_search", lambda q, cfg: ["B", "A"])
    cases = [_case("q1", {"A": 2, "B": 1}), _case("q2", {"B": 1})]  # q2: nobody is marked as the winner
    body = R.evaluate(cases, {"k": 20})
    rows = {q["query_id"]: q for q in body["per_query"]}
    assert rows["q1"]["winner@1"] == 0.0 and rows["q1"]["winner@10"] == 1.0 and rows["q1"]["winner_mrr"] == 0.5
    assert rows["q2"]["winner@10"] is None
    m = body["metrics"]["winner@10"]
    assert m["value"] == 1.0 and m["n"] == 1  # the average is over the cases with a winner, not over all
    assert set(WINNER_METRICS) <= set(body["metrics"])
    assert body["slices"]["channel=ЭМ"]["metrics"]["winner_mrr"]["n"] == 1


def test_evaluate_without_any_winner_has_no_winner_metrics(monkeypatch):
    monkeypatch.setattr(R, "_rank_search", lambda q, cfg: ["A"])
    body = R.evaluate([_case("q1", {"A": 1})], {"k": 20})
    assert not set(WINNER_METRICS) & set(body["metrics"])


def _rec(exp: str, rows: list[dict]) -> dict:
    return {"exp_id": exp, "part": "dev", "per_query": [{"channel": "ЭМ", "recall@10": 0.0, "mrr": 0.0,
                                                          "ndcg@10": 0.0, **r} for r in rows]}


def test_paired_winner_metrics_skip_cases_without_a_winner_and_old_results():
    a = _rec("A", [{"query_id": "q1", "winner@10": 1.0}, {"query_id": "q2", "winner@10": None}])
    b = _rec("B", [{"query_id": "q1", "winner@10": 0.0}, {"query_id": "q2", "winner@10": None}])
    m = C.paired(a, b)["all"]["metrics"]["winner@10"]
    assert m["delta"] == 1.0 and m["n"] == 1
    old = _rec("OLD", [{"query_id": "q1"}, {"query_id": "q2"}])  # a result from before #280 has no winner keys
    assert "winner@10" not in C.paired(a, old)["all"]["metrics"]


def test_backfill_adds_winner_metrics_without_touching_the_ranking(monkeypatch):
    from src.eval import winner_backfill as W

    cases = [_case("q1", {"A": 2, "B": 1}), _case("q2", {"B": 1})]
    monkeypatch.setattr(W, "load_cases", lambda part, cfg: cases)
    rec = {"exp_id": "X", "part": "dev", "commit": "c0ffee", "metrics": {}, "slices": {}, "per_query": [
        {"query_id": "q1", "recall@10": 0.5, "mrr": 0.5, "ndcg@10": 0.5, "channel": "ЭМ", "ranked": ["B", "A"]},
        {"query_id": "q2", "recall@10": 1.0, "mrr": 1.0, "ndcg@10": 1.0, "channel": "ЭМ", "ranked": ["B"]}]}
    out = W.add_winner_metrics(rec)
    assert out["commit"] == "c0ffee" and [q["ranked"] for q in out["per_query"]] == [["B", "A"], ["B"]]
    assert out["metrics"]["winner@10"]["n"] == 1 and out["metrics"]["winner_mrr"]["value"] == 0.5
    assert "winner@10" not in rec["metrics"]  # the input is not changed
    monkeypatch.setattr(W, "load_cases", lambda part, cfg: cases[:1])
    with pytest.raises(ValueError, match="не совпадают"):
        W.add_winner_metrics(rec)
    with pytest.raises(ValueError, match="ranked"):
        W.add_winner_metrics({**rec, "per_query": [{"query_id": "q1"}]})


def test_backfill_refuses_a_subset_of_cases_and_other_parts_and_keeps_slices(monkeypatch):
    from src.eval import winner_backfill as W

    cases = [_case("q1", {"A": 2}), _case("q2", {"A": 2})]
    monkeypatch.setattr(W, "load_cases", lambda part, cfg: cases)
    row = {"query_id": "q1", "recall@10": 1.0, "mrr": 1.0, "ndcg@10": 1.0, "channel": "ЭМ", "ranked": ["A"]}
    rec = {"exp_id": "X", "part": "dev", "metrics": {}, "slices": {}, "per_query": [row]}  # q2 is missing
    with pytest.raises(ValueError, match="не совпадают"):
        W.add_winner_metrics(rec)
    with pytest.raises(ValueError, match="только для dev и real"):
        W.add_winner_metrics({**rec, "part": "holdout"})
    full = {**rec, "per_query": [row, {**row, "query_id": "q2"}]}
    out = W.add_winner_metrics(full)
    assert out["slices"]["channel=ЭМ"]["metrics"]["winner@10"]["n"] == 2


def test_summary_bootstrap_runs_over_cases_with_a_winner_only(monkeypatch):
    base = {"recall@10": 0.0, "mrr": 0.0, "ndcg@10": 0.0}
    rows = [{**base, "winner@10": 1.0}, {**base, "winner@10": None}, {**base, "winner@10": None}]
    seen: list[list[float]] = []
    monkeypatch.setattr(R, "bootstrap_ci", lambda vals, *a, **k: seen.append(list(vals)) or (0.0, 1.0))
    m = R._summary(rows)["winner@10"]
    assert m["value"] == 1.0 and m["n"] == 1 and [1.0] in seen  # None is skipped, not counted as 0
