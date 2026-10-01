"""Baselines B-pop / B-inc / B-bm25 and the eval slices (T-08): no lot after as_of is ever counted.

Fixture tests/fixtures/ingest: paper (17.12) lots 1 and 3 before the cutoff 2025-07-01, lot 6
(2025-08-01, customer 7801000051) and lot 7 (2025-10-15, customer 7701000040) after it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from src.contracts import Query
from src.eval import baselines as B
from src.eval import run as R
from src.eval.split import EvalCase

CUTOFF = date(2025, 7, 1)
PAPER = ["17.12.14.110"]
NO_FILL = {"okpd_level": 4, "count": "participations", "fill": False}


@pytest.fixture
def hist(search_data: Path) -> Iterator[None]:
    B.clear_cache()
    yield
    B.clear_cache()


def q(okpd2: list[str], customer: str | None = None, as_of: date | None = CUTOFF, text: str = "бумага") -> Query:
    return Query(query_id="q", text=text, okpd2=okpd2, customer_inn=customer, as_of=as_of)


def case(lot: int, customer: str | None, grades: dict[str, int], channel: str = "ЭМ") -> EvalCase:
    return EvalCase(query=Query(query_id=f"lot-{lot}", text="бумага офисная", okpd2=PAPER, customer_inn=customer,
                                channel=channel),  # type: ignore[arg-type]
                    relevant_inns=list(grades), date=date(2025, 8, 1), grades=grades)


def test_okpd_prefix():
    assert B.okpd_prefix("17.12.14.110", 4) == "17.12"
    assert B.okpd_prefix(" 17.12 ", 4) == "17.12"
    assert B.okpd_prefix("17.12.14.110", 2) == "17"
    assert B.okpd_prefix("17", 4) is None


def test_pop_counts_lots_in_group_before_as_of(hist):
    h = B.history(CUTOFF)
    # lots 1 and 3: 7801000012 twice, the rest once (ties by INN as a string); 7801000013 has a wrong checksum
    assert B.rank_pop(q(PAPER), 20, NO_FILL, h) == ["7801000012", "4701000038", "780100000090", "7801000020"]
    assert B.rank_pop(q(PAPER), 2, NO_FILL, h) == ["7801000012", "4701000038"]


def test_pop_does_not_see_lots_after_cutoff(hist):
    at_cutoff = B.rank(q(PAPER), {"method": "pop", "k": 20, "fill": False})
    assert "470100000088" not in at_cutoff  # its only paper lot (7) is after the cutoff
    everything = B.rank(q(PAPER, as_of=None), {"method": "pop", "k": 20, "fill": False})
    assert "470100000088" in everything


def test_pop_as_of_is_a_strict_boundary(hist):
    # lot 6 is published 2025-08-01: as_of on that day excludes it, the next day includes it
    on_day = B.rank(q(PAPER, as_of=date(2025, 8, 1)), {"method": "pop", "k": 20, "fill": False})
    assert on_day == ["7801000012", "4701000038", "780100000090", "7801000020"]
    next_day = B.rank(q(PAPER, as_of=date(2025, 8, 2)), {"method": "pop", "k": 20, "fill": False})
    assert next_day == ["7801000012", "780100000090", "4701000038", "7801000020"]


def test_pop_by_wins(hist):
    h = B.history(CUTOFF)
    assert B.rank_pop(q(PAPER), 20, {**NO_FILL, "count": "wins"}, h) == ["7801000012"]
    with pytest.raises(ValueError, match="count"):
        B.rank_pop(q(PAPER), 20, {**NO_FILL, "count": "money"}, h)


def test_pop_fills_from_class_then_from_all(hist):
    h = B.history(CUTOFF)
    # group 17.29 is empty -> class 17 (the paper lots) -> everybody else by all lots
    assert B.rank_pop(q(["17.29.11.000"]), 20, {"fill": True}, h) == [
        "7801000012", "4701000038", "780100000090", "7801000020", "470100000088"]
    assert B.rank_pop(q(["17.29.11.000"]), 20, NO_FILL, h) == []
    # no OKPD2 at all: overall popularity (3, 3, 2, 1, 1 lots)
    assert B.rank_pop(q([]), 3, {"fill": True}, h) == ["7801000012", "7801000020", "4701000038"]
    assert B.rank_pop(q(PAPER), 20, {"fill": True}, h)[:4] == B.rank_pop(q(PAPER), 20, NO_FILL, h)


def test_inc_puts_customers_own_suppliers_first(hist):
    h = B.history(CUTOFF)
    # customer 7701000040 bought paper once (lot 3): its two valid participants, then B-pop
    assert B.rank_inc(q(PAPER, "7701000040"), 20, NO_FILL, h) == ["4701000038", "7801000012"]
    cfg = {"okpd_level": 4, "fill": True}
    assert B.rank_inc(q(PAPER, "7701000040"), 4, cfg, h) == ["4701000038", "7801000012", "780100000090", "7801000020"]
    # customer 7801000051 (lot 1): another order than B-pop
    assert B.rank_inc(q(PAPER, "7801000051"), 4, cfg, h) == ["780100000090", "7801000012", "7801000020", "4701000038"]
    # the incumbent is counted in the query's group only: 7801000051 never bought group 33.12 from 7801000020
    assert B.rank_inc(q(["33.12.18.000"], "7801000051"), 20, NO_FILL, h) == ["470100000088", "4701000038"]


def test_inc_without_customer_history_is_pop(hist):
    h = B.history(CUTOFF)
    cfg = {"okpd_level": 4, "fill": True}
    assert B.rank_inc(q(PAPER, "7800000000"), 20, cfg, h) == B.rank_pop(q(PAPER), 20, cfg, h)
    assert B.rank_inc(q(PAPER, None), 20, cfg, h) == B.rank_pop(q(PAPER), 20, cfg, h)


def test_inc_does_not_see_customers_lots_after_cutoff(hist):
    # 470100000088 met customer 7701000040 only in lot 7 (2025-10-15)
    assert "470100000088" not in B.rank(q(PAPER, "7701000040"), {"method": "inc", "k": 20, "fill": False})
    assert "470100000088" in B.rank(q(PAPER, "7701000040", as_of=None), {"method": "inc", "k": 20, "fill": False})


def test_bm25_is_retriever_order_without_future(hist):
    ranked = B.rank(q([], text="бумага"), {"method": "bm25", "k": 20})
    assert "7801000012" in ranked and "470100000088" not in ranked and "7801000013" not in ranked
    assert "470100000088" in B.rank(q([], text="бумага", as_of=None), {"method": "bm25", "k": 20})
    assert len(B.rank(q([], text="бумага"), {"method": "bm25", "k": 1})) == 1
    assert B.rank(q([], text="щзъхй"), {"method": "bm25", "k": 20}) == []


def test_unknown_method(hist):
    with pytest.raises(ValueError, match="baseline method"):
        B.rank(q(PAPER), {"method": "oracle"})


def test_winner_novelty_on_fixture(hist):
    cases = [
        case(6, "7801000051", {"780100000090": 2, "7801000012": 1}),  # winner took part in lot 1 of this customer
        case(7, "7701000040", {"470100000088": 2, "7801000012": 1}),  # winner seen before, but not at this customer
        case(7, "7701000040", {"470100000088": 1, "7801000012": 1}),  # no winner in the lot
        case(7, None, {"470100000088": 2}),  # no customer
        case(7, "7701000040", {"470100000088": 2, "7801000012": 2}),  # two winners, one is known -> known
    ]
    assert R.winner_novelty(cases, CUTOFF) == [False, True, None, None, False]


def test_winner_novelty_boundary_is_as_of(hist):
    c = [case(7, "7701000040", {"470100000088": 2})]
    assert R.winner_novelty(c, date(2025, 10, 15)) == [True]  # lot 7 itself is not history
    assert R.winner_novelty(c, date(2025, 10, 16)) == [False]


def test_winner_novelty_without_data_is_skipped(tmp_path, monkeypatch):
    B.clear_cache()
    monkeypatch.setenv("DATA_PROCESSED_DIR", str(tmp_path))
    assert R.winner_novelty([case(6, "7801000051", {"780100000090": 2})], CUTOFF) is None


def test_slices_group_by_channel_and_novelty():
    rows = [
        {"recall@10": 1.0, "mrr": 1.0, "ndcg@10": 1.0, "channel": "ЭМ", "winner_new": True},
        {"recall@10": 0.0, "mrr": 0.0, "ndcg@10": 0.0, "channel": "ЭМ", "winner_new": False},
        {"recall@10": 0.5, "mrr": 0.5, "ndcg@10": 0.5, "channel": "АИС ГЗ", "winner_new": True},
        {"recall@10": 0.0, "mrr": 0.0, "ndcg@10": 0.0, "channel": "АИС ГЗ", "winner_new": None},
    ]
    s = R.slices(rows)
    assert {k: v["n_queries"] for k, v in s.items()} == {
        "channel=ЭМ": 2, "channel=АИС ГЗ": 2, "winner=new": 2, "winner=known": 1,
        "channel=ЭМ,winner=new": 1, "channel=ЭМ,winner=known": 1, "channel=АИС ГЗ,winner=new": 1}
    assert s["winner=new"]["metrics"]["recall@10"]["value"] == 0.75
    assert s["channel=АИС ГЗ"]["metrics"]["mrr"]["value"] == 0.25
    lo, hi = s["channel=ЭМ"]["metrics"]["recall@10"]["ci95"]
    assert 0.0 <= lo <= 0.5 <= hi <= 1.0


def test_evaluate_baseline_on_fixture(hist):
    cases = [case(6, "7801000051", {"780100000090": 2, "7801000012": 1}),
             case(7, "7701000040", {"470100000088": 2, "7801000012": 1}, channel="АИС ГЗ")]
    body = R.evaluate(cases, {"method": "inc", "k": 20, "fill": False}, cutoff=CUTOFF)
    first, second = body["per_query"]
    assert first["ranked"] == ["780100000090", "7801000012", "7801000020"] and first["recall@10"] == 1.0
    assert second["ranked"] == ["4701000038", "7801000012"] and second["recall@10"] == 0.5  # winner is new: missed
    assert (first["winner_new"], second["winner_new"]) == (False, True)
    assert (first["channel"], second["channel"]) == ("ЭМ", "АИС ГЗ")
    assert set(body["slices"]) == {"channel=ЭМ", "channel=АИС ГЗ", "winner=new", "winner=known",
                                   "channel=ЭМ,winner=known", "channel=АИС ГЗ,winner=new"}
    assert body["slices"]["winner=new"]["metrics"]["recall@10"]["value"] == 0.5
    assert body["metrics"]["recall@10"]["value"] == 0.75
    assert "ranked" not in R.evaluate(cases, {"method": "inc", "k": 20}, cutoff=CUTOFF, ranked_limit=1)["per_query"][0]


def test_method_config_by_experiment_id():
    assert R.load_method_config("B-pop")["method"] == "pop"
    assert R.load_method_config("B-inc") == {"baseline": "B-inc", "method": "inc", "k": 20, "okpd_level": 4,
                                             "count": "participations", "fill": True}
    assert R.load_method_config("B-bm25")["method"] == "bm25"
    product = R.load_method_config("E-002")  # not a baseline id: the product configuration
    assert "method" not in product and "weights" in product
    assert R.load_method_config("B-inc-real", baseline="B-inc")["method"] == "inc"
    with pytest.raises(ValueError, match="B-none"):
        R.load_method_config("x", baseline="B-none")


def test_dump_record_is_json_with_one_line_per_case():
    rec = {"exp_id": "x", "metrics": {"mrr": {"value": 0.5, "ci95": [0.0, 1.0]}},
           "per_query": [{"query_id": "lot-1", "mrr": 1.0, "ranked": ["7801000012", "4701000038"]},
                         {"query_id": "lot-2", "mrr": 0.0, "ranked": []}]}
    text = R.dump_record(rec)
    assert json.loads(text) == rec
    assert sum("query_id" in line for line in text.splitlines()) == 2
    assert json.loads(R.dump_record({"exp_id": "x", "per_query": []})) == {"exp_id": "x", "per_query": []}


def test_bm25_passes_as_of_to_index_and_retriever(hist, monkeypatch):
    # each of the two alone hides lot 7 on the fixture, so check both: the index (corpus statistics)
    # and the participants filter must be cut at as_of
    from src.match import search as search_mod

    seen: list[tuple[str, date | None]] = []
    real_index, real_retrieve = B.get_index, search_mod._retrieve

    def spy_index(processed, as_of):
        seen.append(("index", as_of))
        return real_index(processed, as_of)

    def spy_retrieve(store, idx, lots, as_of):
        seen.append(("retrieve", as_of))
        return real_retrieve(store, idx, lots, as_of)

    monkeypatch.setattr(B, "get_index", spy_index)
    monkeypatch.setattr(search_mod, "_retrieve", spy_retrieve)
    B.rank(q([], text="бумага"), {"method": "bm25", "k": 20})
    assert seen == [("index", CUTOFF), ("retrieve", CUTOFF)]


def _rec(exp: str, rows: list[tuple[str, float, str, bool | None]]) -> dict:
    return {"exp_id": exp, "part": "dev", "per_query": [
        {"query_id": qid, "recall@10": v, "mrr": v, "ndcg@10": v, "channel": ch, "winner_new": new}
        for qid, v, ch, new in rows]}


def test_compare_is_paired_by_query_id(tmp_path, capsys):
    from src.eval import compare as C

    a = _rec("A", [("q1", 1.0, "ЭМ", True), ("q2", 0.5, "ЭМ", False), ("q3", 0.0, "АИС ГЗ", None)])
    b = _rec("B", [("q3", 1.0, "АИС ГЗ", None), ("q1", 0.0, "ЭМ", True), ("q2", 0.5, "ЭМ", False)])  # other order
    t = C.paired(a, b)
    assert list(t)[0] == "all" and t["all"]["n_queries"] == 3
    assert t["all"]["metrics"]["recall@10"]["delta"] == pytest.approx(0.0)
    assert t["winner=new"]["metrics"]["mrr"]["delta"] == 1.0
    assert t["channel=АИС ГЗ"]["metrics"]["mrr"]["delta"] == -1.0
    assert t["channel=ЭМ"]["metrics"]["ndcg@10"]["delta"] == 0.5
    lo, hi = t["channel=ЭМ"]["metrics"]["ndcg@10"]["ci95"]
    assert 0.0 <= lo <= 0.5 <= hi <= 1.0
    with pytest.raises(ValueError, match="разные наборы"):
        C.paired(a, _rec("B", [("q1", 0.0, "ЭМ", True)]))
    for rec in (a, b):
        (tmp_path / f"{rec['exp_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    assert C.main(["A", "B"], results=tmp_path) == 0
    assert "winner=new" in capsys.readouterr().out


def test_lost_relevant_only_where_other_run_is_better():
    from src.eval.lost import lost_relevant

    a = {"per_query": [{"query_id": "q1", "recall@10": 0.0, "ranked": ["x", "y"]},
                       {"query_id": "q2", "recall@10": 1.0, "ranked": ["r2"]}]}
    b = {"per_query": [{"query_id": "q1", "recall@10": 0.5, "ranked": ["r1", "x", "n1"]},
                       {"query_id": "q2", "recall@10": 1.0, "ranked": ["r2"]}]}
    rel = {"q1": {"r1", "missing"}, "q2": {"r2"}}
    assert lost_relevant(a, b, rel) == [("q1", "r1")]  # n1 is not relevant, q2 is a tie
    assert lost_relevant(b, a, rel) == []


def test_lost_explain_on_fixture(hist, monkeypatch):
    """src/eval/lost.py explains the losses of E-002 (v1): run on `v1` of configs/experiments.yaml (#174)."""
    from src.eval.lost import explain

    v1 = R.load_method_config("v1")
    monkeypatch.setattr(R.search_mod, "load_config", lambda *a, **k: v1)

    c = case(6, "7801000051", {"7802000020": 2})
    assert explain([], {c.query.query_id: c}, CUTOFF)["lost"] == 0
    matches = R.search_mod.search(R.with_cutoff(c.query, CUTOFF), k=200)
    last = matches[-1].counterparty.inn
    assert last is not None
    rec = explain([(c.query.query_id, last)], {c.query.query_id: c}, CUTOFF, k=1)
    assert rec["cases"] == 1 and rec["rank"]["median"] == len(matches)
    assert set(rec["component_delta"]) == {"product_fit", "experience", "activity", "geography"}
    assert sum(rec["largest_weighted_deficit"].values()) == 1 and rec["rerun_differs"] == 0
    # the top-1 supplier against itself: no deficit — a tie, not a product_fit loss; a foreign record differs
    first = matches[0].counterparty.inn
    assert first is not None
    tie = explain([(c.query.query_id, first)], {c.query.query_id: c}, CUTOFF, k=1, recorded={c.query.query_id: ["x"]})
    assert tie["largest_weighted_deficit"] == {"tie_by_inn": 1} and tie["rerun_differs"] == 1


def test_lost_explain_on_the_h15f_default_keeps_geography_and_names_the_tie_break(hist):
    """#197: since D-077 geography has weight 0 — still compared (shown in the card), never the deficit;
    an equal score is ordered by BM25 (tie_break: bm25), so the label is tie_by_bm25, not tie_by_inn."""
    from src.eval.lost import explain

    h15f = R.load_method_config("H-15f", config_of="H-15f")
    assert h15f["weights"]["geography"] == 0.0
    c = case(6, "7801000051", {"7802000020": 2})
    matches = R.search_mod.search(R.with_cutoff(c.query, CUTOFF), k=200, config=h15f)
    first, last = matches[0].counterparty.inn, matches[-1].counterparty.inn
    assert first is not None and last is not None
    rec = explain([(c.query.query_id, last)], {c.query.query_id: c}, CUTOFF, k=1, config=h15f)
    assert "geography" in rec["component_delta"] and "geography" not in rec["largest_weighted_deficit"]
    tie = explain([(c.query.query_id, first)], {c.query.query_id: c}, CUTOFF, k=1, config=h15f)
    assert tie["largest_weighted_deficit"] == {"tie_by_bm25": 1}


def test_lost_reruns_the_recorded_config_not_the_product_default():
    """A record made before `options:` existed (E-002) runs on the v1 defaults, not on today's H-15f options."""
    from src.eval.lost import recorded_config

    rec = {"config": {"retriever": "bm25", "k": 20, "signals": {"incumbent": 0.0},
                      "weights": {"product_fit": 0.35, "experience": 0.2, "reliability": 0.2, "activity": 0.1,
                                  "geography": 0.1, "price": 0.05}}}
    cfg = recorded_config(rec)
    assert cfg["weights"]["geography"] == 0.1 and cfg["options"] == {}
    assert R.search_mod.scoring_options(cfg) == R.search_mod.scoring_options(R.load_method_config("v1", config_of="v1"))
