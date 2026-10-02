"""One «Подбор» pipeline for /api/search, make eval and make predict (#220 №1, src/match/pipeline.py, D-097).

The fixture data of tests/fixtures/ingest; the cached ФНС card is a test double of enrich() — no network.
"""

from datetime import date

import pytest

from src.contracts import Counterparty, Query
from src.enrich import discover as discover_mod
from src.eval import run as R
from src.match import pipeline
from src.match import search as search_mod

LATER = date(2025, 7, 1)  # the cutoff of the fixture (the only prebuilt as_of): both modes see the same data


def _query(**kw) -> Query:
    return Query(**{"query_id": "q", "text": "бумага офисная А4", **kw})


@pytest.fixture
def no_cache(search_data, tmp_path, monkeypatch):
    """An empty source cache and no discovery: only the history block is under test."""
    monkeypatch.setenv("SOURCE_CACHE", str(tmp_path / "sources.sqlite"))
    monkeypatch.setattr(discover_mod, "discover", lambda q, h, **k: discover_mod.Discovery(history_checked=True))
    return search_data


def _liquidated(inn: str):
    """enrich() from the cache: `inn` has a card «ликвидирована» today, the rest have no card."""

    def fake(i, cache=None, offline=False, open_data=True):
        if i != inn:
            raise KeyError(i)
        return Counterparty(inn=i, name="ООО Тест", status="Организация ликвидирована")

    return fake


def test_eval_and_api_rank_the_same_history(no_cache):
    """/api/search mode (cache, «Новые», hard filters) and the backtest give one order on the same data."""
    api = [m.counterparty.inn for m in pipeline.verified(pipeline.pick(_query(as_of=LATER)).history)]
    cfg = R.load_method_config("probe")  # the product configuration
    ev = R._rank_search(_query(as_of=LATER), {**cfg, "k": len(api)})
    assert len(api) > 3 and ev == [inn for inn in api if inn]


def test_eval_top_k_is_the_old_search_top_k(no_cache):
    """With filters={} the backtest list is search(k) itself: earlier E-numbers stay comparable."""
    cfg = R.load_method_config("probe")
    q = _query(as_of=LATER)
    # the old _rank_search: search(k) cut first, then rows without an INN dropped
    old = [m.counterparty.inn for m in search_mod.search(q, k=3, config=cfg)
           if m.origin == "history" and m.counterparty.inn]
    assert R._rank_search(q, {**cfg, "k": 3}) == old


def test_backtest_does_not_depend_on_the_cache_but_the_product_filters(no_cache, monkeypatch):
    cfg = R.load_method_config("probe")
    before = R._rank_search(_query(as_of=LATER), cfg)
    top = before[0]
    monkeypatch.setattr(pipeline.enrich_mod, "enrich", _liquidated(top))
    assert R._rank_search(_query(as_of=LATER), cfg) == before  # today's card does not touch the backtest
    assert top not in [m.counterparty.inn for m in pipeline.pick(_query(as_of=LATER)).history]  # product drops it

    audit = pipeline.TodayAudit()
    audit.add(before, {top})
    rec = audit.record()
    assert rec["legal_dropped"] == 1 and rec["legal_dropped_relevant"] == 1 and rec["cached"] == 1
    assert rec["cases_with_legal_dropped_relevant"] == 1


def test_backtest_needs_as_of_and_refuses_cached_filters(no_cache):
    with pytest.raises(ValueError, match="as_of"):
        pipeline.pick(_query(), backtest=True)  # no as_of: not a backtest
    for f in ({"only_msp": True}, {"kinds": "manufacturer"}, {"only_manufacturers": True}):
        with pytest.raises(ValueError, match="кеша"):
            pipeline.pick(_query(as_of=LATER, filters=f), backtest=True)
    # a filter on organizers' data is fine
    assert pipeline.pick(_query(as_of=LATER, filters={"entity": "ul"}), backtest=True).matches is not None


def test_backtest_never_runs_discovery(no_cache, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("discover() in a backtest")

    monkeypatch.setattr(discover_mod, "discover", boom)
    assert pipeline.pick(_query(as_of=LATER), backtest=True).discovery is None


def test_evaluate_records_the_today_audit_outside_the_metrics(no_cache):
    from src.eval.split import EvalCase

    cfg = R.load_method_config("probe")
    q = _query()  # evaluate() sets as_of = cutoff itself
    top = R._rank_search(_query(as_of=LATER), cfg)
    body = R.evaluate([EvalCase(query=q, relevant_inns=top[:1], date=date(2025, 8, 1))], cfg, cutoff=LATER)
    audit = body["today_filters"]
    assert audit["checked"] == min(10, len(top)) and audit["cached"] == 0 and audit["legal_dropped"] == 0
    assert "today_filters" not in body["metrics"]


def test_rank_reason_compares_with_the_neighbour_left_after_the_filters(no_cache, monkeypatch, tmp_path):
    """#278: «выше следующего» names the supplier the customer sees next — after the hard filters removed
    the one in between, not the model's neighbour before them."""
    import json
    import math

    import numpy as np

    from src.match import ranker

    n = len(ranker.NAMES)
    coef = np.zeros(n)
    coef[ranker.NAMES.index("n_lots")] = 1.0  # rank by matched lots only
    model = tmp_path / "lr.json"
    model.write_text(json.dumps({"features": ranker.NAMES, "fill": [0.0] * n, "mean": [0.0] * n, "std": [1.0] * n,
                                 "coef": coef.tolist(), "intercept": 0.0}), encoding="utf-8")
    cfg = {**search_mod.load_config(), "ranker": {"kind": "logreg", "model": str(model)}}
    got: list = []
    search_mod.search(_query(), k=search_mod.CANDIDATES, config={k: v for k, v in cfg.items() if k != "ranker"},
                      features_out=got)
    lots = {inn: round(math.expm1(r[ranker.NAMES.index("n_lots")])) for inn, r in got}
    before = pipeline.pick(_query(), config=cfg).history
    first, second, third = (m.counterparty.inn for m in before[:3])
    assert first and second and third and lots[first] != lots[third]  # preconditions of the fixture
    want = f"выше следующего: лоты с похожими позициями — {lots[first]} против {lots[third]}"
    # a user filter hides the second one: the reason is computed after the user filters too
    user = pipeline.apply_user_filters
    monkeypatch.setattr(pipeline, "apply_user_filters",
                        lambda ms, q: [m for m in user(ms, q) if m.counterparty.inn != second])
    after = pipeline.pick(_query(), config=cfg).history
    assert [m.counterparty.inn for m in after[:2]] == [first, third] and after[0].rank_reason == want
    monkeypatch.setattr(pipeline, "apply_user_filters", user)
    # a hard filter (liquidated today) hides it
    monkeypatch.setattr(pipeline.enrich_mod, "enrich", _liquidated(second))
    after = pipeline.pick(_query(), config=cfg).history
    assert [m.counterparty.inn for m in after[:2]] == [first, third] and after[0].rank_reason == want
