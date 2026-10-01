"""Component price (#105): scale of the query's НМЦК against the supplier's lots before as_of; off by default."""

from __future__ import annotations

from datetime import date

import pytest

from src.contracts import Counterparty, Query
from src.match import search as search_mod
from src.match.search import _price, scoring_options, search

CUTOFF = date(2025, 7, 1)


def _cp(median: float | None) -> Counterparty:
    stats: dict[str, float | int | str] = {} if median is None else {"median_start_price": median}
    return Counterparty(inn="7801000012", name="x", stats=stats)


def _comp(m, name):
    return next(c for c in m.components if c.name == name)


def test_price_scale_values_and_text():
    assert _price(_cp(10_000), 10_000, CUTOFF)[0] == pytest.approx(1.0)  # type: ignore[index]
    v10, why10 = _price(_cp(10_000), 100_000, CUTOFF)  # type: ignore[misc]
    assert v10 == pytest.approx(0.5) and "в 10 раз крупнее" in why10 and "10 000 ₽" in why10
    v_small, why_small = _price(_cp(100_000), 10_000, CUTOFF)  # type: ignore[misc]
    assert v_small == pytest.approx(0.5) and "мельче" in why_small  # symmetric in log scale
    assert _price(_cp(1_000), 1_000_000, CUTOFF)[0] == 0.0  # type: ignore[index]
    assert _price(_cp(10_000), 100_000, CUTOFF, decades=1.0)[0] == pytest.approx(0.0)  # type: ignore[index]
    with pytest.raises(ValueError):
        scoring_options({"options": {"price_decades": 0}})
    assert "сопоставим" in _price(_cp(10_000), 15_000, None)[1]  # type: ignore[index]


@pytest.mark.parametrize(("median", "budget"), [(None, 1000.0), (1000.0, None), (0.0, 1000.0), (1000.0, 0.0)])
def test_price_not_computed_without_data(median, budget):
    assert _price(_cp(median), budget, CUTOFF) is None


def test_option_validates_and_is_off_by_default():
    assert scoring_options(search_mod.load_config())["price"] == "none"
    with pytest.raises(ValueError):
        scoring_options({"options": {"price": "bogus"}})


def test_search_price_component_only_with_option(search_data, monkeypatch):
    q = Query(query_id="q", text="бумага офисная", budget=40_000.0, as_of=CUTOFF)
    off = search(q)
    assert off and all(_comp(m, "price").weight == 0.0 and "Нет данных" in _comp(m, "price").explanation for m in off)
    cfg = search_mod.load_config()
    on_cfg = {**cfg, "weights": {**cfg["weights"], "price": 0.1}, "options": {**cfg["options"], "price": "scale"}}
    on = search(q, config=on_cfg)
    priced = [m for m in on if "НМЦК запроса" in _comp(m, "price").explanation]
    assert priced and all(_comp(m, "price").weight == 0.1 for m in priced)
    assert all(len([c for c in m.components if c.name == "price"]) == 1 for m in on)  # one price component
    m = priced[0]
    assert m.score == pytest.approx(round(sum(c.value * c.weight for c in m.components), 4))


def test_activity_by_similar_lots(search_data):
    q = Query(query_id="q", text="бумага офисная", as_of=CUTOFF)
    cfg = search_mod.load_config()
    on = search(q, config={**cfg, "options": {**cfg["options"], "activity_by": "similar"}})
    off = search(q)
    assert on and all("похожими позициями" in _comp(m, "activity").explanation for m in on)
    assert all("похожими позициями" not in _comp(m, "activity").explanation for m in off)
    for m in on:  # the latest similar lot is never later than the latest participation of any kind
        same = next(x for x in off if x.counterparty.inn == m.counterparty.inn)
        assert _comp(m, "activity").value <= _comp(same, "activity").value + 1e-9
    with pytest.raises(ValueError):
        scoring_options({"options": {"activity_by": "bogus"}})


def test_experience_wins_rate():
    from src.match.search import Candidate, _experience

    c = Candidate("7801000012", 1.0, 20, 1, 1, "x", CUTOFF)
    count, _ = _experience(c, [], CUTOFF, "similar", wins="count")
    rate, why = _experience(c, [], CUTOFF, "similar", wins="rate")
    assert rate < count and "доля побед" in why  # 1 win of 20: low rate
    few = Candidate("7801000012", 1.0, 3, 3, 1, "x", CUTOFF)
    # 3 of 3 won: the wins half is higher than for 1 of 20, though the lots half is lower
    assert _experience(few, [], CUTOFF, "similar", wins="rate")[1].endswith("(доля побед со сглаживанием 80%)")
    with pytest.raises(ValueError):
        scoring_options({"options": {"experience_wins": "bogus"}})
